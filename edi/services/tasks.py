"""
The two long-running pieces of work, as background tasks.

Both used to be the body of an APIView.post(). The logic is unchanged in
substance - same validator, same sync engine, same converter, same ownership
checks, which happen before the job is ever queued - but three things are
different now that it runs behind a job row.

It reports progress, because three minutes of silence is indistinguishable from
a hang. It cannot strand a file: release_file() below is called from the
runner's failure path whatever the exception was, so CONVERTING and VALIDATING
are states with an exit under every outcome. And conversion no longer trusts
that the caller filtered the file list - a file that is already CONVERTED with
an unchanged mapping is skipped rather than silently reconverted, which is what
made one Convert click on a busy client into thirty full conversions.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from conversion.models import ConversionHistory
from files.models import (
    VALIDATED_STATUSES,
    GeneratedFile,
    ProcessingStatus,
    UploadedFile,
    canonical_status,
)

from edi.models import JobKind, ProcessingJob

from .excel_generator import generate_excel
from .ingest import sync_uploaded_file
from .loop_extractor import StreamingParsedFile
from .mapping_store import get_mappings, get_template, headers_for, lock_template
from .parser import EDI834Parser, envelope_facts
from .row_builder import column_kinds, iter_excel_rows
from .validator import validate_834

logger = logging.getLogger("edi.tasks")


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def release_file(job: ProcessingJob, note: str = "") -> None:
    """
    Put the file back into a status the user can act on.

    Called from the runner whenever a job ends badly, including when the worker
    itself died. The rule is simple: an in-flight status is never left behind.
    VALIDATING goes back to UPLOADED so Validate is pressable again; CONVERTING
    goes back to VALIDATED so Convert is, with the reason recorded on the row so
    the screen can explain itself after a refresh.
    """
    record = UploadedFile.objects.filter(pk=job.uploaded_file_id).first()
    if record is None:
        return

    if job.kind == JobKind.CONVERT:
        if record.processing_status == ProcessingStatus.CONVERTING:
            # Back to VALIDATED, or to CONVERTED when an earlier run had already
            # produced a workbook: that workbook is still on disk and still
            # downloadable, and claiming otherwise would hide a real artefact.
            has_output = record.generated_files.exists()
            record.processing_status = (
                ProcessingStatus.CONVERTED if has_output else ProcessingStatus.VALIDATED
            )
        record.conversion_error = (note or "")[:4000]
        record.save(update_fields=["processing_status", "conversion_error"])
        return

    if record.processing_status == ProcessingStatus.VALIDATING:
        record.processing_status = ProcessingStatus.UPLOADED
        record.error_message = (note or "")[:4000]
        record.processing_finished_at = timezone.now()
        record.save(
            update_fields=[
                "processing_status",
                "error_message",
                "processing_finished_at",
            ]
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

DISPLAY_DATE_FORMAT = getattr(settings, "DISPLAY_DATE_FORMAT", "%m-%d-%Y")


def display_date(value):
    if not value:
        return ""
    try:
        return value.strftime(DISPLAY_DATE_FORMAT)
    except AttributeError:
        return str(value)


def _parse_x12_date(value: str):
    value = (value or "").strip()
    if len(value) == 6:  # GS04 in some 004010 files is YYMMDD
        value = "20" + value
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def mapping_snapshot(rules) -> list:
    """
    The rules that ran, flattened to plain JSON.

    Accepts MappingDetail rows or the dicts the API takes, because both reach
    the converter and both have to be recordable. Frozen onto the history row so
    "which mapping produced this workbook" has an answer that does not depend on
    the template still existing in its original form.
    """
    snapshot = []
    for rule in rules:
        if isinstance(rule, dict):
            snapshot.append(
                {
                    "excel_column": rule.get("excel_column", ""),
                    "segment": rule.get("segment", ""),
                    "element": rule.get("element", ""),
                    "qualifier_element": rule.get("qualifier_element", "") or "",
                    "qualifier_value": rule.get("qualifier_value", "") or "",
                    "occurrence": rule.get("occurrence") or 1,
                    "applies_to": rule.get("applies_to") or "BOTH",
                    "transform": rule.get("transform") or "NONE",
                }
            )
        else:
            snapshot.append(
                {
                    "excel_column": rule.excel_column,
                    "segment": rule.segment,
                    "element": rule.element,
                    "qualifier_element": rule.qualifier_element or "",
                    "qualifier_value": rule.qualifier_value or "",
                    "occurrence": rule.occurrence or 1,
                    "applies_to": rule.applies_to,
                    "transform": rule.transform,
                }
            )
    return snapshot


def snapshot_fingerprint(snapshot: list) -> str:
    """
    A stable digest of a rule set, used to answer "has the mapping changed?".

    This is what lets conversion skip a file whose workbook was produced by
    exactly these rules, and what stops the mapping store minting a new template
    version every time somebody presses Convert without touching a dropdown.
    """
    import hashlib
    import json

    canonical = json.dumps(
        sorted(snapshot, key=lambda rule: str(rule.get("excel_column", ""))),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conversion_fingerprint(headers: list, snapshot: list) -> str:
    """The column layout matters as much as the rules; an added blank column changes the workbook."""
    import hashlib

    digest = hashlib.sha256()
    digest.update("|".join(str(h) for h in headers).encode("utf-8"))
    digest.update(snapshot_fingerprint(snapshot).encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def run_validation(job: ProcessingJob, progress) -> dict:
    """
    Structural validation, envelope facts, and - for a file that passes - the
    member sync.

    The sync is the expensive half by a wide margin: on a twelve thousand loop
    file, validation itself is under a second and the sync is about three
    minutes. That is why the progress bar spends most of its life between 20 and
    95, and why the message says which phase is running.
    """
    record = UploadedFile.objects.select_related("client").get(pk=job.uploaded_file_id)
    path = record.stored_file.path

    progress.set(progress=5, message="Reading the interchange", force=True)

    header = []
    header_done = False
    seen = [0]
    total_bytes = record.file_size_bytes or 0

    def capture_header(stream):
        """Collect the pre-INS segments and report progress as the file goes past."""
        nonlocal header_done
        for segment in stream:
            seen[0] += 1
            if not header_done:
                if segment.name == "INS":
                    header_done = True
                else:
                    header.append(segment)
            if seen[0] % 2000 == 0 and total_bytes:
                progress.set(
                    progress=min(18, 5 + seen[0] // 5000),
                    message="Validating structure ({n:,} segments)".format(n=seen[0]),
                )
            yield segment

    from .parser import EDIParseError

    try:
        result = validate_834(capture_header(EDI834Parser(path).iter_segments()))
    except EDIParseError as exc:
        record.processing_status = ProcessingStatus.QUARANTINED
        record.error_message = str(exc)[:4000]
        record.validation_errors = [str(exc)[:500]]
        record.validation_warnings = []
        record.validated_at = timezone.now()
        record.processing_finished_at = timezone.now()
        record.save()
        return {
            "message": "The file could not be parsed as X12.",
            "is_valid": False,
            "status": canonical_status(record.processing_status),
            "uploaded_file_id": record.id,
            "errors": [str(exc)],
            "warnings": [],
        }

    payload = result.as_dict()
    progress.set(progress=20, message="Recording envelope facts", force=True)

    # Envelope facts, recorded here because upload no longer parses. These drive
    # the file-date dropdown, the Info section and the dashboard; without them a
    # file would never acquire a business date.
    facts = envelope_facts(header)
    for field_name in (
        "interchange_control_number",
        "group_control_number",
        "transaction_set_control_number",
        "sender_id",
        "receiver_id",
        "sponsor_name",
    ):
        setattr(record, field_name, facts[field_name][:60])

    already_converted = (
        record.processing_status == ProcessingStatus.CONVERTED and result.is_valid
    )

    record.file_date = _parse_x12_date(facts["file_date"])
    record.segment_count = result.segment_count
    record.member_loop_count = result.member_loop_count
    record.is_full_file = result.is_full_file
    record.processing_status = (
        record.processing_status
        if already_converted
        else (
            ProcessingStatus.VALIDATED if result.is_valid else ProcessingStatus.QUARANTINED
        )
    )
    record.validation_errors = list(result.errors)[:200]
    record.validation_warnings = list(result.warnings)[:200]
    record.validated_at = timezone.now()
    record.error_message = "\n".join(result.errors)[:4000]
    record.processing_finished_at = timezone.now()
    record.save()

    # -------------------------------------------------------------------
    # The member sync. Valid files only, exactly once.
    #
    # Quarantined files never reach it, so a rejected 835 or a truncated
    # interchange cannot write member rows. The daily_statuses guard makes
    # re-validating an already-synced file a no-op rather than a second
    # multi-minute pass.
    # -------------------------------------------------------------------
    sync_summary = {"synced": 0, "failed": 0, "loops": 0, "errors": []}

    if result.is_valid and not record.daily_statuses.exists():
        progress.set(progress=25, message="Syncing members", force=True)
        expected = record.member_loop_count or 0

        # The sync walks the file twice - subscribers, then dependants - so the
        # bar is scaled against two passes and the message counts within the
        # pass. Reporting done/expected against a single pass is what made the
        # old message read "12,000 of 12,000" for the whole second half.
        total_units = max(expected * 2, 1)

        def report(done, phase):
            in_pass = done - expected if done > expected else done
            progress.fraction(
                done,
                total_units,
                floor=25,
                ceiling=95,
                message="{phase} ({done:,} of {total:,} loops)".format(
                    phase=phase,
                    done=min(max(in_pass, 0), expected) if expected else done,
                    total=expected,
                ),
            )

        sync_summary = sync_uploaded_file(
            record, job.owner, job.client, on_progress=report
        )
    elif result.is_valid:
        progress.set(progress=95, message="Already synced; nothing to do", force=True)
    else:
        logger.warning(
            "Upload %s failed validation; not synced to the member tables.", record.id
        )

    payload.update(
        {
            "message": (
                "834 validation passed."
                if result.is_valid
                else "834 validation failed."
            ),
            "uploaded_file_id": record.id,
            "status": canonical_status(record.processing_status),
            "is_valid": result.is_valid,
            "validated_at": record.validated_at.isoformat() if record.validated_at else None,
            "validated_at_display": display_date(record.validated_at),
            "file_date": record.file_date.isoformat() if record.file_date else None,
            "file_date_display": display_date(record.file_date),
            "members_synced": sync_summary.get("synced", 0),
            "members_failed": sync_summary.get("failed", 0),
            "sync_errors": sync_summary.get("errors", [])[:10],
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def run_conversion(job: ProcessingJob, progress) -> dict:
    """
    Produce the workbook.

    The rules come off the job payload, which the endpoint has already saved to
    the user's template, so the mapping that ran is the mapping that is stored.
    """
    record = UploadedFile.objects.select_related("client").get(pk=job.uploaded_file_id)
    payload = job.result or {}
    rules = payload.get("rules") or []
    headers = payload.get("headers") or []
    template_id = payload.get("template_id")
    mapping_source = payload.get("mapping_source") or "TEMPLATE"
    force = bool(payload.get("force"))

    template = get_template(job.owner, template_id, client=job.client) if template_id else None

    if not rules:
        rules = get_mappings(job.owner, template_id, client=job.client)
        if not rules:
            raise ValueError(
                "No mapping rules were supplied and no saved template was found. "
                "Configure the Data Mapping Schema and try again."
            )
        headers = headers or headers_for(rules)

    snapshot = mapping_snapshot(rules)
    fingerprint = conversion_fingerprint(headers, snapshot)

    # -------------------------------------------------------------------
    # Skip work that would produce a byte-identical workbook.
    #
    # The Convert button converts every convertible file, which is right - a
    # mapping change has to reach the files that were already converted, and
    # that is the behaviour the brief asks for. What is not right is
    # regenerating a file whose workbook was produced by exactly these rules
    # against exactly this source, which on a client with thirty files is
    # thirty conversions to produce thirty identical outputs.
    # -------------------------------------------------------------------
    if not force:
        previous = (
            ConversionHistory.objects.filter(
                uploaded_file=record,
                status__in=(ConversionHistory.Status.SUCCESS, ConversionHistory.Status.PARTIAL),
                generated_file__isnull=False,
            )
            .order_by("-created_at")
            .first()
        )
        if previous and conversion_fingerprint(
            previous.result_headers, previous.mapping_snapshot or []
        ) == fingerprint:
            generated = previous.generated_file
            record.processing_status = ProcessingStatus.CONVERTED
            record.conversion_error = ""
            record.save(update_fields=["processing_status", "conversion_error"])
            return {
                "message": "The mapping has not changed since the last conversion.",
                "skipped": True,
                "uploaded_file_id": record.id,
                "status": canonical_status(record.processing_status),
                "generated_file_id": generated.id,
                "file": generated.generated_filename,
                "download_url": "/api/edi/download/{pk}/".format(pk=generated.id),
                "preview_url": "/api/edi/download/{pk}/preview/".format(pk=generated.id),
                "rows_generated": generated.row_count,
                "conversion_id": previous.id,
                "mapping_version": previous.mapping_version,
            }

    kinds = column_kinds(rules)
    source_path = record.stored_file.path

    UploadedFile.objects.filter(pk=record.pk).update(
        processing_status=ProcessingStatus.CONVERTING, conversion_error=""
    )

    history = ConversionHistory.objects.create(
        owner=job.owner,
        client=job.client,
        uploaded_file=record,
        mapping_template=template,
        mapping_version=template.version if template else None,
        mapping_snapshot=snapshot,
        mapping_source=mapping_source,
        result_headers=list(headers),
        status=ConversionHistory.Status.RUNNING,
        started_at=timezone.now(),
    )

    warnings = []
    expected_rows = record.member_loop_count or 0
    written = [0]

    def rows_with_progress(stream):
        """Drain warnings and report progress as rows go past, holding nothing."""
        for row in stream:
            warnings.extend(row.pop("__warnings__", []))
            written[0] += 1
            if written[0] % 500 == 0:
                progress.fraction(
                    written[0],
                    max(expected_rows, written[0]),
                    floor=5,
                    ceiling=90,
                    message="Writing rows ({n:,})".format(n=written[0]),
                )
            yield row

    progress.set(progress=5, message="Reading the interchange", force=True)

    try:
        parser = EDI834Parser(source_path)
        parsed = StreamingParsedFile(parser.iter_segments())
        rows = rows_with_progress(
            iter_excel_rows(parsed, rules, header_segments=parsed.header)
        )
        workbook = generate_excel(
            headers,
            rows,
            owner_id=job.owner_id,
            source_name=record.original_filename,
            column_kinds=kinds,
        )
    except Exception as exc:  # noqa: BLE001
        history.status = ConversionHistory.Status.FAILED
        history.error_message = "{kind}: {exc}".format(kind=type(exc).__name__, exc=exc)[:4000]
        history.finished_at = timezone.now()
        history.save()
        raise  # the runner records it, releases the file and reports it

    progress.set(progress=92, message="Saving the workbook", force=True)

    generated = GeneratedFile.objects.create(
        owner=job.owner,
        client=job.client,
        uploaded_file=record,
        generated_filename=workbook.filename,
        stored_file=workbook.relative_path,
        row_count=workbook.row_count,
        file_size_bytes=workbook.size_bytes,
    )

    history.generated_file = generated
    history.status = (
        ConversionHistory.Status.PARTIAL if warnings else ConversionHistory.Status.SUCCESS
    )
    history.members_processed = parsed.subscriber_count
    history.dependents_processed = parsed.dependent_count
    history.rows_written = workbook.row_count
    history.warning_count = len(warnings)
    history.warnings = warnings[:500]
    history.finished_at = timezone.now()
    history.save()

    # Freeze the version that produced this workbook, so a later edit clones to
    # the next version rather than rewriting what this history row points at.
    lock_template(template)

    record.processing_status = ProcessingStatus.CONVERTED
    record.converted_at = timezone.now()
    record.conversion_error = ""
    record.save(update_fields=["processing_status", "converted_at", "conversion_error"])

    return {
        "message": "834 converted successfully.",
        "skipped": False,
        "conversion_id": history.id,
        "uploaded_file_id": record.id,
        "status": canonical_status(record.processing_status),
        "converted_at": record.converted_at.isoformat(),
        "converted_at_display": display_date(record.converted_at),
        "generated_file_id": generated.id,
        "file": workbook.filename,
        "download_url": "/api/edi/download/{pk}/".format(pk=generated.id),
        "preview_url": "/api/edi/download/{pk}/preview/".format(pk=generated.id),
        "headers": list(headers),
        "rows_generated": workbook.row_count,
        "subscribers": parsed.subscriber_count,
        "dependents": parsed.dependent_count,
        "mapping_template_id": template.id if template else None,
        "mapping_version": template.version if template else None,
        "mapping_source": mapping_source,
        "mapping_columns": len(snapshot),
        "warning_count": len(warnings),
        "warnings": warnings[:25],
    }

"""
EDI endpoints.

The through-line of this module is that the backend, not the browser, decides
what is true. Three specific consequences, each of which was previously the
other way round:

  * A file is synced into the member tables only when validation passed. The
    old code set QUARANTINED and then called sync_uploaded_file() anyway, so an
    835 or a truncated 834 still wrote eligibility history.

  * Conversion checks the stored validation status itself rather than trusting
    that the UI disabled a button. The convert endpoint is reachable with any
    file id an authenticated user can guess at.

  * The workbook the user downloads is the workbook the server generated. There
    is no second conversion engine anywhere in the request path.

Ownership is checked on every object retrieval, and where the deployment has
clients configured the selected client narrows it further.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from django.db import transaction
from django.db.models import F
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings

from conversion.models import ConversionHistory
from files.models import (
    VALIDATED_STATUSES,
    GeneratedFile,
    ProcessingStatus,
    UploadedFile,
    canonical_status,
)
from files.models import sha256_of
from mapping.models import SegmentElement
from members.models import Dependant, Subscriber
from users.tenancy import resolve_client, scope_to_client

from edi.models import ACTIVE_STATES, JobKind, JobState, ProcessingJob
from edi.services.file_service import UnsafePathError, get_file_path
from edi.services.mapping_store import (
    get_mappings,
    get_template,
    headers_for,
    layout_for,
    save_mapping,
    save_mappings,
)
from edi.services.runner import enqueue, reap_stale
from edi.services.tasks import (
    conversion_fingerprint,
    mapping_snapshot,
    run_conversion,
    run_validation,
)

from .serializers import ConvertRequestSerializer, EDIFileUploadSerializer, MappingSerializer

logger = logging.getLogger("edi.api")

# The only statuses from which a file may be converted. VALIDATED is the
# canonical one; PARSED is its legacy spelling and CONVERTED is here because
# re-running a conversion after a mapping change is a normal thing to do and
# refusing it would mean re-uploading the file to change one column.
CONVERTIBLE_STATUSES = VALIDATED_STATUSES

MAX_PREVIEW_ROWS = 500

# Streaming chunk for the raw-source download. Large enough that a 200 MB file
# is not a million syscalls, small enough that it is never resident.
DOWNLOAD_CHUNK = 64 * 1024

# How many characters of an 834 the preview endpoint returns. The preview is a
# convenience for the on-screen viewer; the download endpoint is the file.
PREVIEW_CHAR_LIMIT = 512 * 1024

DISPLAY_DATE_FORMAT = getattr(settings, "DISPLAY_DATE_FORMAT", "%m-%d-%Y")


def display_date(value):
    """
    MM-DD-YYYY, or empty.

    Part 4 asked for one date format across the workbook, the API and the
    screen. Dates still travel as ISO in their own fields so anything that
    sorts or filters keeps working; these are the strings the UI renders, which
    is what stops each component inventing its own toLocaleDateString call and
    each one getting a slightly different answer.
    """
    if not value:
        return ""
    try:
        return value.strftime(DISPLAY_DATE_FORMAT)
    except AttributeError:
        return str(value)


def owned_uploads(request, client):
    """Every uploaded-file query in this module starts here."""
    return scope_to_client(
        UploadedFile.objects.filter(owner=request.user), client
    )


def owned_generated(request, client):
    return scope_to_client(
        GeneratedFile.objects.filter(owner=request.user), client
    )


def _upload_payload(record, current_fingerprint=None):
    """
    One uploaded file, complete enough that a browser refresh restores the screen.

    Issue 1 in full. The row already held the parse result; what it did not hold
    was the validation outcome, the conversion outcome or a route back to the
    workbook, so those three lived in React and died with it. Everything the
    conversion screen renders now comes from here, which means the answer to
    "what happened to this file" is the same whether you ask it a second after
    the upload or a week later from another machine.
    """
    latest = record.generated_files.order_by("-generated_at").first()
    status = canonical_status(record.processing_status)

    jobs = list(record.jobs.order_by("-created_at")[:4])
    active = next((job for job in jobs if job.state in ACTIVE_STATES), None)
    last = jobs[0] if jobs else None

    last_conversion = (
        record.conversions.filter(generated_file__isnull=False)
        .order_by("-created_at")
        .first()
    )
    stale = False
    if last_conversion is not None and current_fingerprint is not None:
        stale = (
            conversion_fingerprint(
                last_conversion.result_headers, last_conversion.mapping_snapshot or []
            )
            != current_fingerprint
        )
    return {
        "id": record.id,
        "uploaded_file_id": record.id,
        "fileName": record.original_filename,
        "original_filename": record.original_filename,
        "status": status,
        "is_valid": record.processing_status in VALIDATED_STATUSES,
        "is_converted": status == ProcessingStatus.CONVERTED,
        "records": record.member_loop_count or 0,
        "member_loop_count": record.member_loop_count,
        "segment_count": record.segment_count,
        "is_full_file": record.is_full_file,
        "file_date": record.file_date,
        "file_date_display": display_date(record.file_date),
        "file_size_bytes": record.file_size_bytes,
        "file_path": record.stored_file.name,
        "uploaded_at": record.uploaded_at,
        "uploaded_at_display": display_date(record.uploaded_at),
        "validated_at": record.validated_at,
        "validated_at_display": display_date(record.validated_at),
        "converted_at": record.converted_at,
        "converted_at_display": display_date(record.converted_at),
        "error_message": record.error_message,
        "conversion_error": record.conversion_error,
        "errors": record.validation_errors or [],
        "warnings": record.validation_warnings or [],
        "sponsor_name": record.sponsor_name,
        "generated_file_id": latest.id if latest else None,
        "generated_filename": latest.generated_filename if latest else None,
        "generated_row_count": latest.row_count if latest else None,
        "download_url": (
            "/api/edi/download/{pk}/".format(pk=latest.id) if latest else None
        ),
        "preview_url": (
            "/api/edi/download/{pk}/preview/".format(pk=latest.id) if latest else None
        ),
        "generated_file_size_bytes": latest.file_size_bytes if latest else None,
        # Part 8: preview and download are different endpoints because they are
        # different jobs. One returns a readable excerpt, the other returns the
        # bytes that arrived.
        "source_url": "/api/edi/files/{pk}/preview/".format(pk=record.id),
        "source_download_url": "/api/edi/files/{pk}/download/".format(pk=record.id),
        # The in-flight job for this file, if any. This is what lets a browser
        # refresh mid-validation reattach to the run and keep showing progress
        # instead of presenting a file that looks frozen at VALIDATING.
        "active_job": active.as_dict() if active else None,
        "last_job": last.as_dict() if last and not active else None,
        # True when the workbook on disk was produced by a different mapping
        # from the one currently saved. The screen uses it to say "mapping
        # changed - reconvert" rather than leaving the user to guess whether the
        # file they are about to send a client reflects the edit they just made.
        "mapping_stale": stale,
        "converted_mapping_version": last_conversion.mapping_version if last_conversion else None,
    }


class HealthCheckView(APIView):
    permission_classes = []  # a health probe cannot authenticate

    def get(self, request):
        return Response({"status": "healthy", "service": "834 EDI Converter"})


class EDIUploadView(APIView):
    """Store the file, checksum it, validate it, and record what arrived."""

    def post(self, request):
        serializer = EDIFileUploadSerializer(data=request.data)

        if not serializer.is_valid():
            logger.error("Upload validation failed: %s", serializer.errors)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        client = resolve_client(request)
        upload = serializer.validated_data["file"]

        # ---------------------------------------------------------------
        # Issue 9: MAX_834_UPLOAD_BYTES was a setting nothing read.
        #
        # Checked here rather than only in the serializer because a chunked
        # upload can report one size and deliver another, and because the
        # message wants to name the actual limit — "file too large" with no
        # number is the kind of error that generates a support ticket instead
        # of resolving one.
        # ---------------------------------------------------------------
        limit = getattr(settings, "MAX_834_UPLOAD_BYTES", 200 * 1024 * 1024)
        size = getattr(upload, "size", None) or 0
        if size > limit:
            return Response(
                {
                    "detail": (
                        "{name} is {actual:.1f} MB. The largest 834 this portal accepts "
                        "is {limit:.0f} MB. Split the interchange or ask an administrator "
                        "to raise MAX_834_UPLOAD_BYTES."
                    ).format(
                        name=upload.name,
                        actual=size / (1024 * 1024),
                        limit=limit / (1024 * 1024),
                    ),
                    "file_size_bytes": size,
                    "max_upload_bytes": limit,
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        checksum = sha256_of(upload)

        existing = (
            UploadedFile.objects.filter(owner=request.user, content_sha256=checksum)
            .filter(client=client)
            .first()
        )

        if existing and existing.processing_status not in (
            ProcessingStatus.FAILED,
            ProcessingStatus.PENDING,
            ProcessingStatus.UPLOADED,
            ProcessingStatus.PARSING,
            ProcessingStatus.VALIDATING,
        ):
            # A file that was processed to a conclusion, valid or quarantined.
            # Re-uploading it is a no-op, which is the point of the checksum.
            return Response(
                {
                    "message": "This file has already been uploaded.",
                    "uploaded_file_id": existing.id,
                    "file_path": existing.stored_file.name,
                    "status": canonical_status(existing.processing_status),
                    "is_valid": existing.processing_status in VALIDATED_STATUSES,
                    "duplicate": True,
                },
                status=status.HTTP_200_OK,
            )

        if existing:
            # Issue 19: the first attempt died. Duplicate protection was
            # rejecting the retry, which left the user with a permanently
            # unusable file and no way to clear it — the checksum matched, so
            # every subsequent upload bounced off the same broken record.
            # Reuse the row so the audit trail keeps one history for one file.
            record = existing
            record.stored_file.save(upload.name, upload, save=False)
            record.original_filename = upload.name[:255]
            record.file_size_bytes = upload.size
            record.error_message = ""
            record.conversion_error = ""
            record.validation_errors = []
            record.validation_warnings = []
            record.validated_at = None
            record.processing_status = ProcessingStatus.UPLOADED
            record.processing_started_at = None
            record.processing_finished_at = None
            record.save()
            retried = True
        else:
            record = UploadedFile(
                owner=request.user,
                client=client,
                original_filename=upload.name[:255],
                file_size_bytes=upload.size,
                content_sha256=checksum,
                processing_status=ProcessingStatus.UPLOADED,
            )
            record.stored_file.save(upload.name, upload, save=False)
            record.save()
            retried = False

        # -----------------------------------------------------------------
        # Upload now ends here, deliberately.
        #
        # It used to continue into streaming validation and the member sync
        # engine, in this request. On a real-sized 834 — tens of thousands of
        # INS loops — the sync alone runs for minutes, so the browser sat on
        # "Uploading…" until a proxy or a person gave up, and a page refresh
        # then revealed that the file had in fact been stored the whole time.
        # Storing the bytes is fast; everything slow is now behind the
        # Validate button, where the user has asked for it and can watch it.
        # -----------------------------------------------------------------
        return Response(
            {
                "message": "File uploaded. Click Validate to run 834 validation.",
                "uploaded_file_id": record.id,
                "status": canonical_status(record.processing_status),
                "file_path": record.stored_file.name,
                "file_size_bytes": record.file_size_bytes,
                "duplicate": False,
                "retried": retried,
                "is_valid": None,
            },
            status=status.HTTP_201_CREATED,
        )


class UploadListView(APIView):
    """
    The current user's uploads.

    The conversion screen kept its file list in React state alone, so a browser
    refresh emptied a table whose rows were sitting in the database the whole
    time. This is what it reloads from.
    """

    def get(self, request):
        reap_stale()
        client = resolve_client(request)
        records = (
            owned_uploads(request, client)
            .prefetch_related("generated_files", "jobs", "conversions")
            .order_by("-uploaded_at")[:200]
        )

        # Worked out once for the whole list. Per row it would be one hash of
        # the same thirty rules per file on screen.
        details = get_mappings(request.user, None, client=client)
        current = (
            conversion_fingerprint(headers_for(details), mapping_snapshot(details))
            if details
            else None
        )
        return Response([_upload_payload(record, current) for record in records])


class UploadSourceView(APIView):
    """
    The raw 834 as it was stored, for the EDI viewer.

    Owner and client checked here, because the viewer used to fall back to a
    hard-coded sample interchange when it had no content in hand. A real file
    name above somebody else's demo member data is a worse outcome than an
    error message.
    """

    def get(self, request, pk):
        client = resolve_client(request)
        record = owned_uploads(request, client).filter(pk=pk).first()
        if not record:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            with open(
                record.stored_file.path, "r", encoding="utf-8-sig", errors="replace"
            ) as handle:
                content = handle.read(4 * 1024 * 1024)
        except OSError as exc:
            logger.warning("Could not read stored source for upload %s: %s", pk, exc)
            return Response(
                {"detail": "The stored source file could not be read."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "uploaded_file_id": record.id,
                "file_name": record.original_filename,
                "status": record.processing_status,
                "truncated": len(content) >= 4 * 1024 * 1024,
                "content": content,
            }
        )


def _active_job(record, kind):
    """The queued or running job of this kind for this file, if there is one."""
    return (
        ProcessingJob.objects.filter(
            uploaded_file=record, kind=kind, state__in=ACTIVE_STATES
        )
        .order_by("-created_at")
        .first()
    )


def _job_response(job, http_status=status.HTTP_202_ACCEPTED):
    """
    What an enqueued endpoint returns.

    202 when the work has been accepted and not done. The browser polls
    /api/edi/jobs/ and reads the finished job's `result`, which is the exact
    body the endpoint used to return synchronously - so nothing that consumed
    the old response had to learn a new shape, it just learned to wait.

    A job that is already finished by the time the response is built - inline
    mode, or a second call against a run that landed in between - returns 200
    with the result merged into the top level. That keeps one contract for both
    paths: a caller that only cares about the answer reads the same fields it
    always did, and a caller that wants to watch reads job_id and state.
    """
    payload = job.as_dict()
    if not job.is_active and isinstance(job.result, dict):
        merged = dict(job.result)
        merged.update(payload)
        merged["result"] = job.result
        return Response(merged, status=status.HTTP_200_OK)
    return Response(payload, status=http_status)


class ValidateView(APIView):
    """
    Start 834 validation. Returns at once; the work runs in the background.

    This endpoint used to do everything inline: streaming structural validation,
    the envelope facts, and the member sync that populates Member, Subscriber
    and Dependant. On the sample files that is a third of a second. On a real
    interchange it is not - a 2.3 MB file with twelve thousand INS loops
    measured at 173 seconds, essentially all of it in the sync. The browser held
    the request open for the whole of it and showed "Validating...", any proxy in
    front of Django cut the connection first, and refreshing the page then
    revealed the file already VALIDATED. The work was never the problem; waiting
    on it in an HTTP request was.
    """

    def post(self, request):
        client = resolve_client(request)
        record = None

        if request.data.get("uploaded_file_id"):
            record = (
                owned_uploads(request, client)
                .filter(pk=request.data["uploaded_file_id"])
                .first()
            )
            if not record:
                return Response({"detail": "Unknown uploaded_file_id."}, status=404)
        else:
            try:
                path = get_file_path(request.data.get("file_path", ""))
            except UnsafePathError as exc:
                return Response(
                    {"file_path": str(exc)}, status=status.HTTP_400_BAD_REQUEST
                )
            record = (
                owned_uploads(request, client).filter(stored_file=request.data.get("file_path")).first()
            )
            if not record:
                return Response(
                    {
                        "file_path": (
                            "That path does not correspond to a file you uploaded. "
                            "Supply uploaded_file_id instead."
                        )
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Pressing Validate twice must not start two syncs over the same file.
        existing = _active_job(record, JobKind.VALIDATE)
        if existing:
            return _job_response(existing, status.HTTP_200_OK)

        job = ProcessingJob.objects.create(
            owner=request.user,
            client=client,
            uploaded_file=record,
            kind=JobKind.VALIDATE,
            message="Queued",
        )

        UploadedFile.objects.filter(pk=record.pk).update(
            processing_status=ProcessingStatus.VALIDATING,
            processing_started_at=record.processing_started_at or timezone.now(),
            error_message="",
        )

        enqueue(job, run_validation)
        return _job_response(job)


class JobStatusView(APIView):
    """
    What the browser polls.

    Accepts ?ids=1,2,3 for the jobs a screen is watching, or ?active=1 for
    everything still in flight - which is what a freshly loaded page asks for,
    because a refresh mid-run must reattach to the work rather than lose it.

    reap_stale() runs here rather than on a scheduler. It is cheap (one indexed
    query), it runs exactly when somebody is looking, and it means a job whose
    worker was killed is reported as interrupted to the person waiting on it
    instead of appearing to run for ever.
    """

    def get(self, request):
        reap_stale()
        client = resolve_client(request)

        queryset = scope_to_client(
            ProcessingJob.objects.filter(owner=request.user), client
        )

        raw_ids = (request.query_params.get("ids") or "").strip()
        if raw_ids:
            ids = [int(part) for part in raw_ids.split(",") if part.strip().isdigit()]
            queryset = queryset.filter(pk__in=ids[:100])
        elif (request.query_params.get("active") or "").lower() in ("1", "true", "yes"):
            queryset = queryset.filter(state__in=ACTIVE_STATES)
        else:
            queryset = queryset.order_by("-created_at")[:25]

        return Response([job.as_dict() for job in queryset])


class JobDetailView(APIView):
    def get(self, request, pk):
        reap_stale()
        client = resolve_client(request)
        job = (
            scope_to_client(ProcessingJob.objects.filter(owner=request.user), client)
            .filter(pk=pk)
            .first()
        )
        if not job:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(job.as_dict())


class MappingCreateView(APIView):
    """Save a column rule to the user's mapping template, and list what is saved."""

    def get(self, request):
        client = resolve_client(request)
        template_id = request.query_params.get("template_id")
        details = get_mappings(request.user, template_id, client=client)
        template = get_template(request.user, template_id, client=client)
        return Response(
            {
                "template": template.mapping_name if template else None,
                "template_id": template.id if template else None,
                "version": template.version if template else None,
                "locked": bool(template and template.is_locked),
                # The whole grid: every column in the layout, each carrying its
                # rule or blanks. Returning only the columns that had rules is
                # what made an un-mapped column disappear from the screen.
                "columns": layout_for(template, details),
            }
        )

    def post(self, request):
        client = resolve_client(request)

        # Two accepted shapes. A bare list of rules is the original contract and
        # still works. {"columns": [...], "mappings": [...]} is what the mapping
        # screen sends, because the screen knows something a rule list cannot
        # express: which columns exist but are deliberately unmapped.
        body = request.data
        layout = None
        if isinstance(body, dict) and ("mappings" in body or "columns" in body):
            layout = list(body.get("columns") or []) or None
            body = body.get("mappings") or []

        many = isinstance(body, list)
        serializer = MappingSerializer(data=body, many=many)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        payload = [dict(rule) for rule in (serializer.validated_data if many else [serializer.validated_data])]
        template_name = (payload[0].get("template_name") or "Default") if payload else "Default"
        for rule in payload:
            rule.pop("template_name", None)

        if many:
            # A whole screenful. The incoming set is the truth for the template:
            # rules for columns the user unmapped are removed, and a version is
            # minted only if anything actually differs from what is stored.
            # Saving these one at a time was how a cleared column stayed mapped
            # and how three identical Convert clicks produced four versions.
            saved = save_mappings(
                payload,
                owner=request.user,
                template_name=template_name,
                client=client,
                columns=layout,
            )
        else:
            single = save_mapping(
                payload[0], owner=request.user, template_name=template_name, client=client
            )
            saved = {
                "template_id": single["template_id"],
                "version": single["version"],
                "changed": True,
                "columns": 1,
            }

        details = get_mappings(request.user, saved["template_id"], client=client)
        template = get_template(request.user, saved["template_id"], client=client)
        return Response(
            {
                "message": "Mapping saved." if saved.get("changed") else "Mapping unchanged.",
                "template_id": saved["template_id"],
                "version": saved["version"],
                "changed": bool(saved.get("changed")),
                "columns": saved.get("columns", 0),
                "mappings": layout_for(template, details),
            },
            status=status.HTTP_201_CREATED,
        )


class Convert834View(APIView):
    """
    Start a conversion. Returns at once; the work runs in the background.

    The endpoint's job is now three things: check that this user may convert
    this file, decide and persist the mapping the run will use, and queue it.
    Everything expensive - parsing, row building, writing the workbook - happens
    in edi.services.tasks.run_conversion behind a job row.

    The mapping is saved here rather than in the task on purpose. Saving is
    fast, it is the thing the user pressed the button to do, and doing it inside
    the request means a failure to save is a 400 the user sees immediately
    rather than a background job that fails for a reason nobody connects to a
    dropdown they changed.
    """

    def post(self, request):
        serializer = ConvertRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        client = resolve_client(request)

        # ---------------------------------------------------------------
        # Resolve the file through the owner-scoped queryset, always. The
        # endpoint is reachable with any id, and the front end blocking a
        # button is not an authorisation control.
        # ---------------------------------------------------------------
        if data.get("uploaded_file_id"):
            record = (
                owned_uploads(request, client)
                .filter(pk=data["uploaded_file_id"])
                .first()
            )
            if not record:
                return Response(
                    {"uploaded_file_id": "Unknown file."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            try:
                get_file_path(data["file_path"])
            except UnsafePathError as exc:
                return Response(
                    {"file_path": str(exc)}, status=status.HTTP_400_BAD_REQUEST
                )
            record = (
                owned_uploads(request, client)
                .filter(stored_file=data["file_path"])
                .first()
            )
            if not record:
                return Response(
                    {
                        "file_path": (
                            "That path does not correspond to a file you uploaded. "
                            "Supply uploaded_file_id instead."
                        )
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

        # The backend enforces the validation gate itself.
        if record.processing_status not in CONVERTIBLE_STATUSES:
            return Response(
                {
                    "detail": (
                        "{name} is in status {s} and cannot be converted. Only a file "
                        "that passed 834 validation may be converted."
                    ).format(name=record.original_filename, s=canonical_status(record.processing_status)),
                    "uploaded_file_id": record.id,
                    "status": canonical_status(record.processing_status),
                    "errors": (record.error_message or "").splitlines()[:25],
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        existing = _active_job(record, JobKind.CONVERT)
        if existing:
            return _job_response(existing, status.HTTP_200_OK)

        # ---------------------------------------------------------------
        # Which rules will run, and making sure they are the stored ones.
        #
        # Inline rules are persisted to the user's template first, so the run
        # has a template id and a version like any other and the audit trail can
        # say how a workbook was produced. save_mappings() compares the incoming
        # set against what is stored and only mints a new version when the
        # mapping genuinely changed - the previous code minted one on every
        # click, because a completed conversion locks the version it used and
        # the next save then cloned all thirty rules to version n+1 whether or
        # not a single dropdown had moved.
        # ---------------------------------------------------------------
        template = None
        mapping_source = "TEMPLATE"

        if data.get("mappings"):
            rules = [dict(rule) for rule in data["mappings"]]
            headers = data["headers"]
            mapping_source = "INLINE"
            try:
                saved = save_mappings(
                    rules,
                    owner=request.user,
                    template_name=(rules[0].get("template_name") or "Default") if rules else "Default",
                    client=client,
                    columns=list(headers),
                )
                template = get_template(request.user, saved["template_id"], client=client)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Could not persist inline mapping rules for upload %s", record.id
                )
                return Response(
                    {
                        "detail": (
                            "The mapping could not be saved, so the conversion was not "
                            "started. {kind}: {exc}"
                        ).format(kind=type(exc).__name__, exc=exc)
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            template = get_template(
                request.user, data.get("mapping_template_id"), client=client
            )
            details = get_mappings(
                request.user, data.get("mapping_template_id"), client=client
            )
            if not details:
                return Response(
                    {
                        "detail": (
                            "No mapping rules were supplied and no saved template was "
                            "found. Configure the Data Mapping Schema first."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rules = [
                {
                    "excel_column": detail.excel_column,
                    "column_order": detail.column_order,
                    "segment": detail.segment,
                    "element": detail.element,
                    "qualifier_element": detail.qualifier_element or "",
                    "qualifier_value": detail.qualifier_value or "",
                    "component_index": detail.component_index,
                    "occurrence": detail.occurrence or 1,
                    "applies_to": detail.applies_to,
                    "transform": detail.transform,
                    "default_value": detail.default_value or "",
                    "is_required": bool(detail.is_required),
                }
                for detail in details
            ]
            headers = data.get("headers") or headers_for(details)

        for rule in rules:
            rule.pop("template_name", None)

        job = ProcessingJob.objects.create(
            owner=request.user,
            client=client,
            uploaded_file=record,
            kind=JobKind.CONVERT,
            message="Queued",
            result={
                "rules": rules,
                "headers": list(headers),
                "template_id": template.id if template else None,
                "mapping_source": mapping_source,
                # force=1 reconverts even when the mapping is unchanged, for the
                # case where the source file was re-uploaded under the same id.
                "force": str(request.query_params.get("force", "")).lower()
                in ("1", "true", "yes")
                or bool(request.data.get("force")),
            },
        )

        UploadedFile.objects.filter(pk=record.pk).update(
            processing_status=ProcessingStatus.CONVERTING, conversion_error=""
        )

        enqueue(job, run_conversion)
        return _job_response(job)


class DownloadView(APIView):
    """
    Retrieve a generated workbook.

    MEDIA_ROOT is not served by the URL conf, deliberately, because these files
    contain PHI and must not be reachable by guessing a path. Ownership is
    checked here and the download is counted for the audit trail.
    """

    def head(self, request, pk):
        """
        Headers only. Never counted, never opened.

        Django maps HEAD onto the GET handler when a view does not define one,
        which is wrong for this endpoint in two ways: it increments
        downloaded_count, and it opens the file to stream a body that is then
        discarded. The browser download path issues a HEAD first so that a 403
        or a 404 surfaces as an error the screen can show rather than as an
        error page rendered inside a hidden iframe - and that preflight was
        therefore recording a second download every time. On a system holding
        PHI, downloaded_count is the record of who took data out and how often;
        doubling it makes the one number an auditor asks for untrue.
        """
        client = resolve_client(request)
        generated = owned_generated(request, client).filter(pk=pk).first()
        if not generated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = 'attachment; filename="{name}"'.format(
            name=generated.generated_filename
        )
        if generated.file_size_bytes:
            response["Content-Length"] = str(generated.file_size_bytes)
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def get(self, request, pk):
        client = resolve_client(request)
        generated = owned_generated(request, client).filter(pk=pk).first()
        if not generated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # F() rather than read-modify-write: two downloads racing would
        # otherwise both read the same count and both write the same successor,
        # recording one.
        GeneratedFile.objects.filter(pk=pk).update(
            downloaded_count=F("downloaded_count") + 1,
            last_downloaded_at=timezone.now(),
        )
        return FileResponse(
            generated.stored_file.open("rb"),
            as_attachment=True,
            filename=generated.generated_filename,
        )


class GeneratedFilePreviewView(APIView):
    """
    Read the generated workbook back and return its cells as JSON.

    This exists so the Excel preview shows the file that was actually produced.
    The browser previously re-parsed the raw 834 with a second, qualifier-blind
    converter, which meant the preview and the download could disagree — and
    when they disagreed the preview was the one the user believed.
    """

    def get(self, request, pk):
        from openpyxl import load_workbook

        client = resolve_client(request)
        generated = owned_generated(request, client).filter(pk=pk).first()
        if not generated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = min(int(request.query_params.get("limit", 100)), MAX_PREVIEW_ROWS)
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        try:
            workbook = load_workbook(
                generated.stored_file.path, read_only=True, data_only=True
            )
        except (OSError, KeyError, ValueError) as exc:
            logger.warning("Could not read generated workbook %s: %s", pk, exc)
            return Response(
                {"detail": "The generated workbook could not be read."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            sheet = workbook.worksheets[0]
            iterator = sheet.iter_rows(values_only=True)
            headers = [
                "" if value is None else str(value) for value in next(iterator, ()) or ()
            ]
            # Skipped with the iterator rather than by materialising and
            # slicing: read_only mode streams the sheet, so paging to row
            # 40,000 costs a walk and not 40,000 dicts.
            for _ in range(offset):
                if next(iterator, None) is None:
                    break

            rows = []
            for values in iterator:
                if len(rows) >= limit:
                    break
                rows.append(
                    {
                        header: ("" if value is None else str(value))
                        for header, value in zip(headers, values)
                    }
                )
        finally:
            workbook.close()

        return Response(
            {
                "generated_file_id": generated.id,
                "file_name": generated.generated_filename,
                "source_file": generated.uploaded_file.original_filename,
                "headers": headers,
                "rows": rows,
                "row_count": generated.row_count,
                "offset": offset,
                "returned": len(rows),
                "has_more": bool(
                    generated.row_count and offset + len(rows) < generated.row_count
                ),
                "truncated": bool(
                    generated.row_count and generated.row_count > offset + len(rows)
                ),
                "file_size_bytes": generated.file_size_bytes,
                "download_url": "/api/edi/download/{pk}/".format(pk=generated.id),
            }
        )


class ConversionHistoryView(APIView):
    def get(self, request):
        client = resolve_client(request)
        history = scope_to_client(
            ConversionHistory.objects.filter(owner=request.user), client
        ).select_related("uploaded_file", "generated_file", "mapping_template")[:100]

        return Response(
            [
                {
                    "id": item.id,
                    "uploaded_file_id": item.uploaded_file_id,
                    "source_file": item.uploaded_file.original_filename,
                    "original_file": item.uploaded_file.original_filename,
                    "generated_file": (
                        item.generated_file.generated_filename
                        if item.generated_file_id
                        else None
                    ),
                    "generated_file_id": item.generated_file_id,
                    "file_date": item.uploaded_file.file_date,
                    "file_date_display": display_date(item.uploaded_file.file_date),
                    "created_at_display": display_date(item.created_at),
                    "mapping_template": (
                        item.mapping_template.mapping_name
                        if item.mapping_template
                        else None
                    ),
                    "mapping_version": item.mapping_version,
                    "mapping_source": item.mapping_source,
                    "mapping_columns": len(item.mapping_snapshot or []),
                    "status": item.status,
                    "rows_written": item.rows_written,
                    "members": item.members_processed,
                    "dependents": item.dependents_processed,
                    "warning_count": item.warning_count,
                    "error_message": item.error_message,
                    "created_at": item.created_at,
                    "download_url": (
                        "/api/edi/download/{pk}/".format(pk=item.generated_file_id)
                        if item.generated_file_id
                        else None
                    ),
                    "preview_url": (
                        "/api/edi/download/{pk}/preview/".format(pk=item.generated_file_id)
                        if item.generated_file_id
                        else None
                    ),
                    "source_url": "/api/edi/uploads/{pk}/content/".format(
                        pk=item.uploaded_file_id
                    ),
                }
                for item in history
            ]
        )


# ===========================================================================
# Part 8 — the raw 834: preview and download are separate endpoints.
#
# They were one endpoint that read four megabytes into a string and returned it
# as a JSON field. That is a reasonable preview and a broken download: anything
# larger was silently truncated, and the truncation was reported in a "truncated"
# flag the download path never looked at. A client who downloaded a 60 MB
# interchange received the first four megabytes of it, in a file with the right
# name, with no error anywhere. Splitting them makes the contract explicit —
# preview returns an excerpt and says so; download returns the file.
# ===========================================================================


class EDIFilePreviewView(APIView):
    """
    A readable excerpt of the stored 834, for the on-screen viewer.

    Truncation is expected here and is reported honestly, with the byte offsets
    so a caller can page through a large file rather than guessing.
    """

    def get(self, request, pk):
        client = resolve_client(request)
        record = owned_uploads(request, client).filter(pk=pk).first()
        if not record:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = min(
                int(request.query_params.get("limit", PREVIEW_CHAR_LIMIT)),
                PREVIEW_CHAR_LIMIT,
            )
        except (TypeError, ValueError):
            limit = PREVIEW_CHAR_LIMIT
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        try:
            with open(
                record.stored_file.path, "r", encoding="utf-8-sig", errors="replace"
            ) as handle:
                if offset:
                    handle.seek(offset)
                content = handle.read(limit)
        except OSError as exc:
            logger.warning("Could not read stored source for upload %s: %s", pk, exc)
            return Response(
                {"detail": "The stored source file could not be read."},
                status=status.HTTP_404_NOT_FOUND,
            )

        total = record.file_size_bytes or 0
        returned = len(content)
        return Response(
            {
                "uploaded_file_id": record.id,
                "file_name": record.original_filename,
                "status": canonical_status(record.processing_status),
                "file_size_bytes": total,
                "offset": offset,
                "returned_chars": returned,
                "truncated": bool(total and offset + returned < total),
                "download_url": "/api/edi/files/{pk}/download/".format(pk=record.id),
                "content": content,
            }
        )


class EDIFileDownloadView(APIView):
    """
    The stored 834, whole, streamed.

    FileResponse with an open handle rather than read() into a Response: the
    file is never resident, which is the only way a 200 MB interchange is
    servable from a process that also has to answer other requests.
    """

    def head(self, request, pk):
        """Headers only, without opening the file. See DownloadView.head."""
        client = resolve_client(request)
        record = owned_uploads(request, client).filter(pk=pk).first()
        if not record:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(content_type="application/octet-stream")
        response["Content-Disposition"] = 'attachment; filename="{name}"'.format(
            name=record.original_filename
        )
        if record.file_size_bytes:
            response["Content-Length"] = str(record.file_size_bytes)
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def get(self, request, pk):
        client = resolve_client(request)
        record = owned_uploads(request, client).filter(pk=pk).first()
        if not record:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            handle = record.stored_file.open("rb")
        except (OSError, ValueError) as exc:
            logger.warning("Could not open stored source for upload %s: %s", pk, exc)
            return Response(
                {"detail": "The stored source file could not be read."},
                status=status.HTTP_404_NOT_FOUND,
            )

        response = FileResponse(
            handle,
            as_attachment=True,
            filename=record.original_filename,
            content_type="application/octet-stream",
        )
        # Set explicitly so a browser shows real progress instead of an
        # indeterminate spinner on a file that takes a minute to arrive.
        if record.file_size_bytes:
            response["Content-Length"] = str(record.file_size_bytes)
        response["X-Content-Type-Options"] = "nosniff"
        return response


# ===========================================================================
# Part 6.4 — the segment dictionary comes from the database.
# ===========================================================================


class SegmentDictionaryView(APIView):
    """
    Every 834 segment and element the mapping UI may offer.

    The dropdowns were a literal in a React file, so the set of segments a user
    could map was whatever somebody had typed into the front end — nine of them
    — while SegmentElement sat seeded and migrated in the database with the
    rest. Adding a segment meant a front-end release. It is a management
    command now, or a row.
    """

    def get(self, request):
        include_inactive = (request.query_params.get("include_inactive") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        queryset = SegmentElement.objects.all()
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        segments: dict = {}
        for item in queryset.order_by("segment_name", "element_code"):
            bucket = segments.setdefault(item.segment_name, [])
            # The dictionary is unique on (segment, element, loop), so the same
            # element can legitimately appear under two loops. The dropdown
            # wants one entry per element.
            if any(entry["code"] == item.element_code for entry in bucket):
                continue
            position = item.element_code[len(item.segment_name):]
            bucket.append(
                {
                    # Hyphenated for display, canonical for the wire. Both are
                    # sent so the browser never has to derive one from the
                    # other and get it subtly wrong.
                    "id": "{seg}-{pos}".format(seg=item.segment_name, pos=position),
                    "code": item.element_code,
                    "name": item.description,
                    "loop_id": item.loop_id,
                    "data_type": item.data_type,
                }
            )

        return Response(
            {
                "segments": segments,
                "segment_names": sorted(segments.keys()),
                "count": sum(len(v) for v in segments.values()),
            }
        )


# ===========================================================================
# Part 5 — the Information section, from the database.
# ===========================================================================


class DashboardSummaryView(APIView):
    """
    The counts behind the Information panel.

    Every number here was a literal in the markup. A dashboard whose figures do
    not move is worse than no dashboard: it is read as a measurement, and it
    reports the same reassuring totals whether the last upload succeeded, failed
    or never happened.

    Counted in the database rather than in Python. The alternative — fetch the
    rows and len() them — is fine at demo scale and is a full table read per
    tile once a sponsor has thirty thousand members.
    """

    def get(self, request):
        from django.db.models import Count, Max, Q, Sum

        client = resolve_client(request)
        uploads = owned_uploads(request, client)

        counts = uploads.aggregate(
            total=Count("id"),
            validated=Count(
                "id", filter=Q(processing_status__in=VALIDATED_STATUSES)
            ),
            converted=Count("id", filter=Q(processing_status=ProcessingStatus.CONVERTED)),
            failed=Count("id", filter=Q(processing_status=ProcessingStatus.FAILED)),
            quarantined=Count(
                "id", filter=Q(processing_status=ProcessingStatus.QUARANTINED)
            ),
            pending=Count(
                "id",
                filter=Q(
                    processing_status__in=(
                        ProcessingStatus.UPLOADED,
                        ProcessingStatus.PENDING,
                        ProcessingStatus.VALIDATING,
                        ProcessingStatus.PARSING,
                        ProcessingStatus.CONVERTING,
                    )
                ),
            ),
            latest_upload=Max("uploaded_at"),
            latest_file_date=Max("file_date"),
            member_loops=Sum("member_loop_count"),
        )

        subscribers = scope_to_client(
            Subscriber.objects.filter(owner=request.user), client
        ).count()
        dependants = scope_to_client(
            Dependant.objects.filter(owner=request.user), client
        ).count()

        conversions = scope_to_client(
            ConversionHistory.objects.filter(owner=request.user), client
        )
        conversion_counts = conversions.aggregate(
            runs=Count("id"),
            failed_runs=Count("id", filter=Q(status=ConversionHistory.Status.FAILED)),
            rows=Sum("rows_written"),
        )

        latest_upload = counts["latest_upload"]

        return Response(
            {
                "total_files": counts["total"] or 0,
                "validated_files": counts["validated"] or 0,
                "converted_files": counts["converted"] or 0,
                "failed_files": counts["failed"] or 0,
                "quarantined_files": counts["quarantined"] or 0,
                "in_progress_files": counts["pending"] or 0,
                "total_subscribers": subscribers,
                "total_dependants": dependants,
                "total_members": subscribers + dependants,
                "total_member_loops": counts["member_loops"] or 0,
                "conversion_runs": conversion_counts["runs"] or 0,
                "failed_conversions": conversion_counts["failed_runs"] or 0,
                "rows_generated": conversion_counts["rows"] or 0,
                "latest_upload_at": latest_upload,
                "latest_upload_display": display_date(latest_upload),
                "latest_file_date": counts["latest_file_date"],
                "latest_file_date_display": display_date(counts["latest_file_date"]),
            }
        )

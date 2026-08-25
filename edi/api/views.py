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
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from conversion.models import ConversionHistory
from files.models import GeneratedFile, ProcessingStatus, UploadedFile
from files.models import sha256_of
from users.tenancy import resolve_client, scope_to_client

from edi.services.excel_generator import generate_excel
from edi.services.file_service import UnsafePathError, get_file_path
from edi.services.ingest import sync_uploaded_file
from edi.services.loop_extractor import StreamingParsedFile
from edi.services.mapping_store import (
    get_mappings,
    get_template,
    headers_for,
    lock_template,
    save_mapping,
)
from edi.services.parser import EDI834Parser, EDIParseError, envelope_facts
from edi.services.row_builder import iter_excel_rows
from edi.services.validator import validate_834

from .serializers import ConvertRequestSerializer, EDIFileUploadSerializer, MappingSerializer

logger = logging.getLogger("edi.api")

# The only status from which a file may be converted. Kept as a tuple so a
# future PARSED_WITH_WARNINGS or similar can be added in one place.
CONVERTIBLE_STATUSES = (ProcessingStatus.PARSED,)

MAX_PREVIEW_ROWS = 500


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


def owned_uploads(request, client):
    """Every uploaded-file query in this module starts here."""
    return scope_to_client(
        UploadedFile.objects.filter(owner=request.user), client
    )


def owned_generated(request, client):
    return scope_to_client(
        GeneratedFile.objects.filter(owner=request.user), client
    )


def _upload_payload(record):
    """
    One uploaded file as the conversion screen needs it.

    Includes the newest generated workbook so a page refresh can restore the
    download link rather than losing it with the React state.
    """
    latest = record.generated_files.order_by("-generated_at").first()
    return {
        "id": record.id,
        "uploaded_file_id": record.id,
        "fileName": record.original_filename,
        "original_filename": record.original_filename,
        "status": record.processing_status,
        "is_valid": record.processing_status == ProcessingStatus.PARSED,
        "records": record.member_loop_count or 0,
        "member_loop_count": record.member_loop_count,
        "segment_count": record.segment_count,
        "is_full_file": record.is_full_file,
        "file_date": record.file_date,
        "file_path": record.stored_file.name,
        "uploaded_at": record.uploaded_at,
        "error_message": record.error_message,
        "sponsor_name": record.sponsor_name,
        "generated_file_id": latest.id if latest else None,
        "generated_filename": latest.generated_filename if latest else None,
        "download_url": (
            "/api/edi/download/{pk}/".format(pk=latest.id) if latest else None
        ),
        "source_url": "/api/edi/uploads/{pk}/content/".format(pk=record.id),
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
        checksum = sha256_of(upload)

        existing = (
            UploadedFile.objects.filter(owner=request.user, content_sha256=checksum)
            .filter(client=client)
            .first()
        )

        if existing and existing.processing_status not in (
            ProcessingStatus.FAILED,
            ProcessingStatus.PENDING,
            ProcessingStatus.PARSING,
        ):
            # A file that was processed to a conclusion, valid or quarantined.
            # Re-uploading it is a no-op, which is the point of the checksum.
            return Response(
                {
                    "message": "This file has already been uploaded.",
                    "uploaded_file_id": existing.id,
                    "file_path": existing.stored_file.name,
                    "status": existing.processing_status,
                    "is_valid": existing.processing_status == ProcessingStatus.PARSED,
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
            record.processing_status = ProcessingStatus.PENDING
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
                processing_status=ProcessingStatus.PENDING,
            )
            record.stored_file.save(upload.name, upload, save=False)
            record.save()
            retried = False

        return self._process(request, record, client, retried)

    def _process(self, request, record, client, retried):
        try:
            record.processing_status = ProcessingStatus.PARSING
            record.processing_started_at = timezone.now()
            record.save(update_fields=["processing_status", "processing_started_at"])

            parser = EDI834Parser(record.stored_file.path)

            header = []
            header_done = False

            def capture_header(stream):
                nonlocal header_done
                for segment in stream:
                    if not header_done:
                        if segment.name == "INS":
                            header_done = True
                        else:
                            header.append(segment)
                    yield segment

            result = validate_834(capture_header(parser.iter_segments()))

            if result is None:
                raise RuntimeError(
                    "validate_834() returned None instead of a validation result."
                )

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

            record.file_date = _parse_x12_date(facts["file_date"])
            record.segment_count = result.segment_count
            record.member_loop_count = result.member_loop_count
            record.is_full_file = result.is_full_file

            record.processing_status = (
                ProcessingStatus.PARSED
                if result.is_valid
                else ProcessingStatus.QUARANTINED
            )
            record.error_message = "\n".join(result.errors)[:4000]
            record.processing_finished_at = timezone.now()
            record.save()

            # ---------------------------------------------------------
            # DATABASE SYNC ENGINE — valid files only.
            #
            # The old code ran this unconditionally, right after setting the
            # status to QUARANTINED. So an 835, a truncated interchange or a
            # file with mismatched control numbers still wrote Member and
            # MemberDailyStatus rows, and those rows then answered eligibility
            # questions as if the file had been trustworthy. Quarantine that
            # does not actually quarantine anything is worse than none, because
            # it reads as a control that is working.
            # ---------------------------------------------------------
            sync_summary = {"synced": 0, "failed": 0, "loops": 0, "errors": []}
            if result.is_valid:
                try:
                    sync_summary = sync_uploaded_file(record, request.user, client)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Member sync failed for upload %s", record.id)
                    sync_summary["errors"] = [
                        "{k}: {e}".format(k=type(exc).__name__, e=exc)
                    ]
            else:
                logger.warning(
                    "Upload %s failed validation; not synced to the member tables.",
                    record.id,
                )

            return Response(
                {
                    "message": (
                        "File uploaded and parsed successfully."
                        if result.is_valid
                        else "File was rejected by 834 validation and has been quarantined."
                    ),
                    "uploaded_file_id": record.id,
                    "status": record.processing_status,
                    "member_loop_count": record.member_loop_count,
                    "segment_count": record.segment_count,
                    "is_full_file": record.is_full_file,
                    "file_path": record.stored_file.name,
                    "file_date": record.file_date,
                    "duplicate": False,
                    "retried": retried,
                    "is_valid": result.is_valid,
                    "errors": result.errors[:25],
                    "warnings": result.warnings[:25],
                    "transaction_count": result.transaction_count,
                    "members_synced": sync_summary.get("synced", 0),
                    "members_failed": sync_summary.get("failed", 0),
                    "sync_errors": sync_summary.get("errors", [])[:10],
                },
                status=status.HTTP_201_CREATED,
            )

        except (EDIParseError, OSError) as exc:
            record.processing_status = ProcessingStatus.FAILED
            record.error_message = str(exc)[:4000]
            record.processing_finished_at = timezone.now()
            record.save(
                update_fields=[
                    "processing_status",
                    "error_message",
                    "processing_finished_at",
                ]
            )
            logger.warning("upload %s failed to parse: %s", record.id, exc)
            return Response(
                {
                    "message": "File stored but could not be parsed.",
                    "uploaded_file_id": record.id,
                    "status": record.processing_status,
                    "is_valid": False,
                    "error": str(exc),
                    "retryable": True,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as exc:  # noqa: BLE001
            record.processing_status = ProcessingStatus.FAILED
            record.error_message = "{t}: {e}".format(t=type(exc).__name__, e=exc)[:4000]
            record.processing_finished_at = timezone.now()
            record.save(
                update_fields=[
                    "processing_status",
                    "error_message",
                    "processing_finished_at",
                ]
            )
            logger.exception("Unexpected error while processing upload %s", record.id)
            return Response(
                {
                    "message": "An unexpected error occurred while processing the file.",
                    "uploaded_file_id": record.id,
                    "status": record.processing_status,
                    "is_valid": False,
                    "retryable": True,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class UploadListView(APIView):
    """
    The current user's uploads.

    The conversion screen kept its file list in React state alone, so a browser
    refresh emptied a table whose rows were sitting in the database the whole
    time. This is what it reloads from.
    """

    def get(self, request):
        client = resolve_client(request)
        records = (
            owned_uploads(request, client)
            .prefetch_related("generated_files")
            .order_by("-uploaded_at")[:200]
        )
        return Response([_upload_payload(record) for record in records])


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


class ValidateView(APIView):
    """Structural validation on its own, so a user can check a file before converting."""

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
            path = record.stored_file.path
        else:
            try:
                path = get_file_path(request.data.get("file_path", ""))
            except UnsafePathError as exc:
                return Response(
                    {"file_path": str(exc)}, status=status.HTTP_400_BAD_REQUEST
                )

        try:
            result = validate_834(EDI834Parser(path).iter_segments())
        except EDIParseError as exc:
            if record:
                record.processing_status = ProcessingStatus.QUARANTINED
                record.error_message = str(exc)[:4000]
                record.save(update_fields=["processing_status", "error_message"])
            return Response(
                {"is_valid": False, "errors": [str(exc)]}, status=status.HTTP_200_OK
            )

        payload = result.as_dict()

        if record:
            # Keep the stored status honest. A file that fails validation here
            # must not still be sitting at PARSED, because PARSED is what the
            # convert endpoint checks.
            record.processing_status = (
                ProcessingStatus.PARSED if result.is_valid else ProcessingStatus.QUARANTINED
            )
            record.error_message = "\n".join(result.errors)[:4000]
            record.save(update_fields=["processing_status", "error_message"])
            payload["uploaded_file_id"] = record.id
            payload["status"] = record.processing_status

        return Response(payload)


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
                "columns": [
                    {
                        "excel_column": detail.excel_column,
                        "column_order": detail.column_order,
                        "segment": detail.segment,
                        "element": detail.element,
                        "qualifier_element": detail.qualifier_element,
                        "qualifier_value": detail.qualifier_value,
                        "occurrence": detail.occurrence,
                        "applies_to": detail.applies_to,
                        "transform": detail.transform,
                    }
                    for detail in details
                ],
            }
        )

    def post(self, request):
        client = resolve_client(request)
        many = isinstance(request.data, list)
        serializer = MappingSerializer(data=request.data, many=many)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        payload = serializer.validated_data if many else [serializer.validated_data]
        saved = []
        with transaction.atomic():
            for rule in payload:
                saved.append(
                    save_mapping(
                        rule,
                        owner=request.user,
                        template_name=rule.get("template_name", "Default"),
                        client=client,
                    )
                )

        return Response(
            {
                "message": "Mapping saved",
                "template_id": saved[0]["template_id"] if saved else None,
                "version": saved[0]["version"] if saved else None,
                "mappings": saved,
            },
            status=status.HTTP_201_CREATED,
        )


class Convert834View(APIView):
    """Run the full pipeline and record the result."""

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

        # Issue 10: the backend enforces the validation gate itself.
        if record.processing_status not in CONVERTIBLE_STATUSES:
            return Response(
                {
                    "detail": (
                        "This file is in status {s} and cannot be converted. Only a file "
                        "that passed 834 validation may be converted."
                    ).format(s=record.processing_status),
                    "uploaded_file_id": record.id,
                    "status": record.processing_status,
                    "errors": (record.error_message or "").splitlines()[:25],
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        source_path = record.stored_file.path

        # Mapping rules: inline if supplied, otherwise the saved template.
        template = None
        if data.get("mappings"):
            rules = data["mappings"]
            headers = data["headers"]
        else:
            template = get_template(
                request.user, data.get("mapping_template_id"), client=client
            )
            rules = get_mappings(
                request.user, data.get("mapping_template_id"), client=client
            )
            if not rules:
                return Response(
                    {"detail": "No mapping rules supplied and no saved template found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            headers = data.get("headers") or headers_for(rules)

        history = ConversionHistory.objects.create(
            owner=request.user,
            client=client,
            uploaded_file=record,
            mapping_template=template,
            mapping_version=template.version if template else None,
            status=ConversionHistory.Status.RUNNING,
            started_at=timezone.now(),
        )

        warnings = []

        def rows_with_warnings(stream):
            """Drain warnings as rows go past so nothing is held for a second pass."""
            for row in stream:
                warnings.extend(row.pop("__warnings__", []))
                yield row

        try:
            parser = EDI834Parser(source_path)
            parsed = StreamingParsedFile(parser.iter_segments())

            rows = rows_with_warnings(
                iter_excel_rows(parsed, rules, header_segments=parsed.header)
            )

            workbook = generate_excel(
                headers,
                rows,
                owner_id=request.user.id,
                source_name=record.original_filename,
            )
        except (EDIParseError, ValueError, OSError) as exc:
            history.status = ConversionHistory.Status.FAILED
            history.error_message = str(exc)[:4000]
            history.finished_at = timezone.now()
            history.save()
            logger.exception("conversion failed for %s", source_path)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        generated = GeneratedFile.objects.create(
            owner=request.user,
            client=client,
            uploaded_file=record,
            generated_filename=workbook.filename,
            stored_file=workbook.relative_path,
            row_count=workbook.row_count,
            file_size_bytes=workbook.size_bytes,
        )

        subscribers = parsed.subscriber_count
        history.generated_file = generated
        history.status = (
            ConversionHistory.Status.PARTIAL if warnings else ConversionHistory.Status.SUCCESS
        )
        history.members_processed = subscribers
        history.dependents_processed = parsed.dependent_count
        history.rows_written = workbook.row_count
        history.warning_count = len(warnings)
        history.warnings = warnings[:500]
        history.finished_at = timezone.now()
        history.save()

        # Issue 16: the version that produced this workbook is now frozen. A
        # later edit clones to the next version rather than rewriting what this
        # history row points at.
        lock_template(template)

        return Response(
            {
                "message": "834 converted successfully",
                "conversion_id": history.id,
                "generated_file_id": generated.id,
                "file": workbook.filename,
                "download_url": "/api/edi/download/{pk}/".format(pk=generated.id),
                "preview_url": "/api/edi/download/{pk}/preview/".format(pk=generated.id),
                "headers": list(headers),
                "rows_generated": workbook.row_count,
                "subscribers": subscribers,
                "dependents": parsed.dependent_count,
                "mapping_template_id": template.id if template else None,
                "mapping_version": template.version if template else None,
                "warning_count": len(warnings),
                "warnings": warnings[:25],
            }
        )


class DownloadView(APIView):
    """
    Retrieve a generated workbook.

    MEDIA_ROOT is not served by the URL conf, deliberately, because these files
    contain PHI and must not be reachable by guessing a path. Ownership is
    checked here and the download is counted for the audit trail.
    """

    def get(self, request, pk):
        client = resolve_client(request)
        generated = owned_generated(request, client).filter(pk=pk).first()
        if not generated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        GeneratedFile.objects.filter(pk=pk).update(
            downloaded_count=generated.downloaded_count + 1,
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
                "returned": len(rows),
                "truncated": bool(generated.row_count and generated.row_count > len(rows)),
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
                    "mapping_template": (
                        item.mapping_template.mapping_name
                        if item.mapping_template
                        else None
                    ),
                    "mapping_version": item.mapping_version,
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

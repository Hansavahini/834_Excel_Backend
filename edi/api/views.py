"""
EDI endpoints.

What changed and why:

  * Upload wrote the file to storage and returned a path. Nothing was recorded
    in UploadedFile, so the models the project already had — checksum, envelope
    facts, processing status, member counts — stayed empty forever and the
    stated requirement to "maintain conversion history" was not met by any code
    path. Upload now creates the row.

  * Convert took a client-supplied path straight to open(). See file_service for
    why that was both broken and unsafe.

  * Convert never ran the validator, never wrote a GeneratedFile row, never
    wrote a ConversionHistory row, and left the workbook in the process working
    directory under a fixed name that concurrent users overwrote.

  * There was no download endpoint at all, so even a correctly generated
    workbook could not be retrieved.
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

from edi.services.excel_generator import generate_excel
from edi.services.file_service import UnsafePathError, get_file_path
from edi.services.loop_extractor import StreamingParsedFile
from edi.services.mapping_store import get_mappings, get_template, headers_for, save_mapping
from edi.services.parser import EDI834Parser, EDIParseError, envelope_facts
from edi.services.row_builder import iter_excel_rows
from edi.services.validator import validate_834

from .serializers import ConvertRequestSerializer, EDIFileUploadSerializer, MappingSerializer

logger = logging.getLogger("edi.api")


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


class HealthCheckView(APIView):
    permission_classes = []  # a health probe cannot authenticate

    def get(self, request):
        return Response({"status": "healthy", "service": "834 EDI Converter"})


class EDIUploadView(APIView):
    """Store the file, checksum it, validate it, and record what arrived."""

    def post(self, request):
        serializer = EDIFileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        upload = serializer.validated_data["file"]
        checksum = sha256_of(upload)

        existing = UploadedFile.objects.filter(
            owner=request.user, content_sha256=checksum
        ).first()
        if existing:
            # The unique constraint would raise an IntegrityError here; a
            # re-upload of an identical file is a normal event, not an error.
            return Response(
                {
                    "message": "This file has already been uploaded.",
                    "uploaded_file_id": existing.id,
                    "file_path": existing.stored_file.name,
                    "status": existing.processing_status,
                    "duplicate": True,
                },
                status=status.HTTP_200_OK,
            )

        record = UploadedFile(
            owner=request.user,
            original_filename=upload.name[:255],
            file_size_bytes=upload.size,
            content_sha256=checksum,
            processing_status=ProcessingStatus.PENDING,
        )
        record.stored_file.save(upload.name, upload, save=False)
        record.save()

        try:
            record.processing_status = ProcessingStatus.PARSING
            record.processing_started_at = timezone.now()
            record.save(update_fields=["processing_status", "processing_started_at"])

            parser = EDI834Parser(record.stored_file.path)

            # One streaming pass. Materialising every segment to validate and
            # then walking the list again for the envelope cost ~150 MB on a
            # 6 MB file; the header is all envelope_facts needs, so capture it
            # on the way past and let the rest be garbage collected.
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
                ProcessingStatus.PARSED if result.is_valid else ProcessingStatus.QUARANTINED
            )
            record.error_message = "\n".join(result.errors)[:4000]
            record.processing_finished_at = timezone.now()
            record.save()

        except (EDIParseError, OSError) as exc:
            record.processing_status = ProcessingStatus.FAILED
            record.error_message = str(exc)[:4000]
            record.processing_finished_at = timezone.now()
            record.save()
            logger.warning("upload %s failed to parse: %s", record.id, exc)
            return Response(
                {"message": "File stored but could not be parsed.", "uploaded_file_id": record.id,
                 "error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": "834 file uploaded successfully",
                "uploaded_file_id": record.id,
                "file_path": record.stored_file.name,
                "status": record.processing_status,
                "sponsor": record.sponsor_name,
                "file_date": record.file_date,
                "segment_count": record.segment_count,
                "member_loop_count": record.member_loop_count,
                "is_full_file": record.is_full_file,
                "validation": result.as_dict(),
            },
            status=status.HTTP_201_CREATED,
        )


class ValidateView(APIView):
    """Structural validation on its own, so a user can check a file before converting."""

    def post(self, request):
        record = None
        if request.data.get("uploaded_file_id"):
            record = UploadedFile.objects.filter(
                pk=request.data["uploaded_file_id"], owner=request.user
            ).first()
            if not record:
                return Response({"detail": "Unknown uploaded_file_id."}, status=404)
            path = record.stored_file.path
        else:
            try:
                path = get_file_path(request.data.get("file_path", ""))
            except UnsafePathError as exc:
                return Response({"file_path": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = validate_834(EDI834Parser(path).iter_segments())
        except EDIParseError as exc:
            return Response({"is_valid": False, "errors": [str(exc)]}, status=status.HTTP_200_OK)

        return Response(result.as_dict())


class MappingCreateView(APIView):
    """Save a column rule to the user's mapping template, and list what is saved."""

    def get(self, request):
        details = get_mappings(request.user, request.query_params.get("template_id"))
        template = get_template(request.user, request.query_params.get("template_id"))
        return Response(
            {
                "template": template.mapping_name if template else None,
                "template_id": template.id if template else None,
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
        many = isinstance(request.data, list)
        serializer = MappingSerializer(data=request.data, many=many)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        payload = serializer.validated_data if many else [serializer.validated_data]
        saved = []
        with transaction.atomic():
            for rule in payload:
                saved.append(
                    save_mapping(rule, owner=request.user, template_name=rule.get("template_name", "Default"))
                )

        return Response({"message": "Mapping saved", "mappings": saved}, status=status.HTTP_201_CREATED)


class Convert834View(APIView):
    """Run the full pipeline and record the result."""

    def post(self, request):
        serializer = ConvertRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        record = None
        if data.get("uploaded_file_id"):
            record = UploadedFile.objects.filter(
                pk=data["uploaded_file_id"], owner=request.user
            ).first()
            if not record:
                return Response({"uploaded_file_id": "Unknown file."}, status=status.HTTP_404_NOT_FOUND)
            source_path = record.stored_file.path
        else:
            try:
                source_path = get_file_path(data["file_path"])
            except UnsafePathError as exc:
                return Response({"file_path": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            record = UploadedFile.objects.filter(
                owner=request.user, stored_file=data["file_path"]
            ).first()

        # Mapping rules: inline if supplied, otherwise the saved template.
        template = None
        if data.get("mappings"):
            rules = data["mappings"]
            headers = data["headers"]
        else:
            template = get_template(request.user, data.get("mapping_template_id"))
            rules = get_mappings(request.user, data.get("mapping_template_id"))
            if not rules:
                return Response(
                    {"detail": "No mapping rules supplied and no saved template found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            headers = data.get("headers") or headers_for(rules)

        history = None
        if record:
            history = ConversionHistory.objects.create(
                owner=request.user,
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
            # header_segments is a live list that fills before the first loop is
            # yielded, which is exactly when the row builder first reads it.
            rows = rows_with_warnings(
                iter_excel_rows(parsed, rules, header_segments=parsed.header)
            )

            workbook = generate_excel(
                headers,
                rows,
                owner_id=request.user.id,
                source_name=record.original_filename if record else os.path.basename(source_path),
            )
        except (EDIParseError, ValueError, OSError) as exc:
            if history:
                history.status = ConversionHistory.Status.FAILED
                history.error_message = str(exc)[:4000]
                history.finished_at = timezone.now()
                history.save()
            logger.exception("conversion failed for %s", source_path)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        generated = None
        if record:
            generated = GeneratedFile.objects.create(
                owner=request.user,
                uploaded_file=record,
                generated_filename=workbook.filename,
                stored_file=workbook.relative_path,
                row_count=workbook.row_count,
                file_size_bytes=workbook.size_bytes,
            )

        subscribers = parsed.subscriber_count
        if history:
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

        return Response(
            {
                "message": "834 converted successfully",
                "conversion_id": history.id if history else None,
                "generated_file_id": generated.id if generated else None,
                "file": workbook.filename,
                "download_url": (
                    "/api/edi/download/{pk}/".format(pk=generated.id) if generated else None
                ),
                "rows_generated": workbook.row_count,
                "subscribers": subscribers,
                "dependents": parsed.dependent_count,
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
        generated = GeneratedFile.objects.filter(pk=pk, owner=request.user).first()
        if not generated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        GeneratedFile.objects.filter(pk=pk).update(
            downloaded_count=generated.downloaded_count + 1, last_downloaded_at=timezone.now()
        )
        return FileResponse(
            generated.stored_file.open("rb"),
            as_attachment=True,
            filename=generated.generated_filename,
        )


class ConversionHistoryView(APIView):
    def get(self, request):
        history = (
            ConversionHistory.objects.filter(owner=request.user)
            .select_related("uploaded_file", "generated_file", "mapping_template")[:100]
        )
        return Response(
            [
                {
                    "id": item.id,
                    "source_file": item.uploaded_file.original_filename,
                    "file_date": item.uploaded_file.file_date,
                    "mapping_template": item.mapping_template.mapping_name if item.mapping_template else None,
                    "mapping_version": item.mapping_version,
                    "status": item.status,
                    "rows_written": item.rows_written,
                    "members": item.members_processed,
                    "dependents": item.dependents_processed,
                    "warning_count": item.warning_count,
                    "created_at": item.created_at,
                    "download_url": (
                        "/api/edi/download/{pk}/".format(pk=item.generated_file_id)
                        if item.generated_file_id
                        else None
                    ),
                }
                for item in history
            ]
        )

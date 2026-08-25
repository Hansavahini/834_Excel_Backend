"""
files app — inbound 834 artefacts and outbound Excel artefacts.

Nothing binary is stored in the database. Both models hold a path plus the
metadata needed to find, audit and de-duplicate the artefact on disk.
"""

import hashlib

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .db_compat import check_constraint


def upload_to_834(instance, filename):
    """Partition uploads by owner and calendar day so a directory never grows unbounded."""
    return "uploads/{owner}/{stamp:%Y/%m/%d}/{name}".format(
        owner=instance.owner_id or "orphan", stamp=timezone.now(), name=filename
    )


def upload_to_excel(instance, filename):
    return "generated/{owner}/{stamp:%Y/%m/%d}/{name}".format(
        owner=instance.owner_id or "orphan", stamp=timezone.now(), name=filename
    )


def sha256_of(file_object, chunk_size=1024 * 1024):
    """Checksum an uploaded file without loading it into memory."""
    digest = hashlib.sha256()
    file_object.seek(0)
    for chunk in iter(lambda: file_object.read(chunk_size), b""):
        digest.update(chunk)
    file_object.seek(0)
    return digest.hexdigest()


class ProcessingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PARSING = "PARSING", "Parsing"
    PARSED = "PARSED", "Parsed"
    FAILED = "FAILED", "Failed"
    QUARANTINED = "QUARANTINED", "Quarantined"


class UploadedFile(models.Model):
    """One physical 834 file as it arrived, plus the envelope facts read during parse."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_files",
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="uploaded_files",
        help_text="Health plan this record belongs to. Null on rows written before tenancy existed.",
    )
    original_filename = models.CharField(max_length=255)
    stored_file = models.FileField(upload_to=upload_to_834, max_length=500)
    file_size_bytes = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    content_sha256 = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Checksum of the raw bytes. Used to detect a re-upload of an already processed file.",
    )

    # X12 envelope facts, populated by the parser rather than by the upload view.
    interchange_control_number = models.CharField(max_length=9, blank=True, help_text="ISA13")
    group_control_number = models.CharField(max_length=9, blank=True, help_text="GS06")
    transaction_set_control_number = models.CharField(max_length=9, blank=True, help_text="ST02")
    sender_id = models.CharField(max_length=15, blank=True, help_text="ISA06")
    receiver_id = models.CharField(max_length=15, blank=True, help_text="ISA08")
    sponsor_name = models.CharField(max_length=60, blank=True, help_text="Loop 1000A N102")

    file_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Business date the file represents, taken from BGN03 or GS04. Daily comparison keys "
            "on this, never on uploaded_at — a file can arrive late or be re-uploaded."
        ),
    )
    is_full_file = models.BooleanField(
        null=True,
        help_text="True when the file is a full-file audit refresh (INS03=030 throughout), False for change-only files.",
    )

    processing_status = models.CharField(
        max_length=16, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING, db_index=True
    )
    error_message = models.TextField(blank=True)
    segment_count = models.PositiveIntegerField(null=True, blank=True)
    member_loop_count = models.PositiveIntegerField(null=True, blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-uploaded_at",)
        indexes = [
            models.Index(fields=["owner", "-file_date"], name="uf_owner_filedate_idx"),
            models.Index(fields=["owner", "processing_status"], name="uf_owner_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "client", "content_sha256"],
                name="uniq_upload_per_owner_checksum",
            ),
            check_constraint(
                condition=models.Q(processing_finished_at__isnull=True)
                | models.Q(processing_started_at__isnull=False),
                name="uf_finished_requires_started",
            ),
        ]

    def __str__(self):
        return "{name} ({date})".format(name=self.original_filename, date=self.file_date or "undated")


class GeneratedFile(models.Model):
    """An Excel (or CSV) artefact produced from one uploaded file."""

    class Format(models.TextChoices):
        XLSX = "XLSX", "Excel workbook"
        CSV = "CSV", "Comma separated"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="generated_files"
    )
    uploaded_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="generated_files"
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_files",
        help_text="Health plan this record belongs to. Null on rows written before tenancy existed.",
    )
    generated_filename = models.CharField(max_length=255)
    stored_file = models.FileField(upload_to=upload_to_excel, max_length=500)
    file_format = models.CharField(max_length=8, choices=Format.choices, default=Format.XLSX)
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    downloaded_count = models.PositiveIntegerField(default=0)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-generated_at",)
        indexes = [models.Index(fields=["owner", "-generated_at"], name="gf_owner_generated_idx")]

    def __str__(self):
        return self.generated_filename

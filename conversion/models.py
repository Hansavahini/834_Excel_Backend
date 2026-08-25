"""
conversion app — the audit trail for a run, and the result of a daily comparison.

ConversionHistory is deliberately append-only. It is the record that answers
"which file, which mapping version, which workbook, and did it succeed", which
is the question an auditor asks and the question support asks when a client
says the numbers moved.
"""

from django.conf import settings
from django.db import models

from files.db_compat import check_constraint
from files.models import GeneratedFile, UploadedFile
from mapping.models import MappingTemplate


class ConversionHistory(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Completed with warnings"
        FAILED = "FAILED", "Failed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="conversions"
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="conversions",
        help_text="Health plan this record belongs to. Null on rows written before tenancy existed.",
    )
    uploaded_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="conversions"
    )
    mapping_template = models.ForeignKey(
        MappingTemplate, null=True, blank=True, on_delete=models.PROTECT, related_name="conversions"
    )
    mapping_version = models.PositiveIntegerField(
        null=True, blank=True, help_text="Snapshot of the template version used, so later edits do not rewrite history."
    )
    mapping_snapshot = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "The exact rules that produced this workbook, frozen at run time. "
            "Issue 6.2: the browser sent ad-hoc rules and the run recorded no "
            "template at all, so the audit trail said 'converted' without being "
            "able to say converted how. A template id and version answer that "
            "only while the template still exists; this answers it always."
        ),
    )
    mapping_source = models.CharField(
        max_length=16,
        blank=True,
        help_text="TEMPLATE when a saved template drove the run, INLINE when rules came with the request.",
    )
    generated_file = models.ForeignKey(
        GeneratedFile, null=True, blank=True, on_delete=models.PROTECT, related_name="conversions"
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED, db_index=True)
    members_processed = models.PositiveIntegerField(default=0)
    dependents_processed = models.PositiveIntegerField(default=0)
    rows_written = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name_plural = "conversion history"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["owner", "-created_at"], name="ch_owner_created_idx")]
        constraints = [
            check_constraint(
                condition=~models.Q(status="SUCCESS") | models.Q(generated_file__isnull=False),
                name="successful_conversion_has_output",
                violation_error_message="A successful conversion must reference the workbook it produced.",
            )
        ]

    def __str__(self):
        return "Conversion {pk} [{status}]".format(pk=self.pk, status=self.status)


class FileComparison(models.Model):
    """
    Result of comparing one day's file against the previous one. The workflow
    calls for a daily comparison but the original model list had nowhere to put
    the answer, so the UI would have to recompute it on every page load.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="file_comparisons"
    )
    baseline_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="comparisons_as_baseline"
    )
    current_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="comparisons_as_current"
    )
    added_count = models.PositiveIntegerField(default=0)
    terminated_count = models.PositiveIntegerField(default=0)
    changed_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    dropped_count = models.PositiveIntegerField(
        default=0, help_text="Present in baseline, silently absent from current. On a full file this means termination."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["baseline_file", "current_file"], name="uniq_comparison_pair"
            ),
            check_constraint(
                condition=~models.Q(baseline_file=models.F("current_file")),
                name="comparison_needs_two_files",
            ),
        ]

    def __str__(self):
        return "Comparison {a} to {b}".format(a=self.baseline_file_id, b=self.current_file_id)

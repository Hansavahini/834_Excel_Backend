"""
edi app — the record of a background run.

Validation and conversion used to happen inside the HTTP request that asked for
them. On the sample files in this repository that is invisible; on a real 834 it
is not. A 2.3 MB interchange with twelve thousand INS loops takes about three
minutes to validate, because validation also drives the member sync engine, and
conversion of the same file takes seven seconds. The browser sat on
"Validating..." for the whole of it, any proxy in front of Django cut the
connection first, and a page refresh then showed VALIDATED - because the work
had completed server-side the whole time and only the answer was lost.

So the endpoints now enqueue and return immediately, and this model is what the
browser polls. It exists rather than being folded into
UploadedFile.processing_status for three reasons: a job has a progress figure
and a message that the file status cannot carry, a failed job must keep its
error after the file has been reset to a usable status, and a process that is
killed mid-run leaves rows here that the startup sweep in apps.py can find and
mark as interrupted rather than leaving the file stuck at CONVERTING for ever.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class JobKind(models.TextChoices):
    VALIDATE = "VALIDATE", "Validate 834"
    CONVERT = "CONVERT", "Convert to Excel"


class JobState(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    # A worker that died - process restart, container eviction, OOM. Distinct
    # from FAILED because nothing is known about why, and because the right
    # response is "run it again", not "read the error".
    INTERRUPTED = "INTERRUPTED", "Interrupted"


TERMINAL_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.INTERRUPTED)
ACTIVE_STATES = (JobState.QUEUED, JobState.RUNNING)


class ProcessingJob(models.Model):
    """One background run against one uploaded file."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="processing_jobs"
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="processing_jobs",
    )
    uploaded_file = models.ForeignKey(
        "files.UploadedFile", on_delete=models.CASCADE, related_name="jobs"
    )
    kind = models.CharField(max_length=16, choices=JobKind.choices, db_index=True)
    state = models.CharField(
        max_length=16, choices=JobState.choices, default=JobState.QUEUED, db_index=True
    )

    # 0-100. Coarse on purpose: a percentage accurate to the loop would cost a
    # database write per loop, which is more expensive than the work it reports
    # on. The runner throttles updates to a few per second.
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True)

    # Whatever the endpoint would have returned had it run synchronously. The
    # browser reads it out of the finished job, so nothing about the response
    # shape had to change when the work moved off the request thread.
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)

    # Set by the runner every few seconds while it works. A row that is RUNNING
    # with a heartbeat older than the stale window belongs to a worker that is
    # gone, which is how a job is reclaimed without a scheduler.
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="pj_owner_created_idx"),
            models.Index(fields=["uploaded_file", "kind", "-created_at"], name="pj_file_kind_idx"),
            models.Index(fields=["state", "heartbeat_at"], name="pj_state_heartbeat_idx"),
        ]

    def __str__(self):
        return "{kind} job {pk} [{state}]".format(
            kind=self.kind, pk=self.pk, state=self.state
        )

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def duration_seconds(self):
        if not self.started_at:
            return None
        end = self.finished_at or timezone.now()
        return round((end - self.started_at).total_seconds(), 2)

    def as_dict(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "uploaded_file_id": self.uploaded_file_id,
            "result": self.result or {},
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "is_active": self.is_active,
        }

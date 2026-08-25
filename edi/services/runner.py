"""
The background runner.

There is no Celery here and no Redis, deliberately. This portal is a single
Django process serving one TPA's operations team; adding a broker and a second
deployable to move two functions off the request thread would be a larger change
than the problem justifies, and it would be one more thing to be down at three
in the morning. What the work actually needs is: start it without blocking the
response, let the browser watch it, survive a worker dying, and never leave a
file in a status it cannot leave. A bounded thread pool with a database-backed
job row does all four.

What this is not is a claim that threads scale. EDI_WORKER_THREADS is small on
purpose - conversion is CPU-bound in openpyxl and the sync is I/O-bound on
SQLite, and running eight of them at once on one machine makes every one of them
slower. If this deployment ever grows past one process, replace submit() with a
Celery task; ProcessingJob is already the shape a task result needs, and nothing
in the API layer would change.

Three things here are load-bearing.

Connections. A thread that Django did not create owns its own database
connection, and nothing closes it when the thread ends. Every job therefore
closes connections in a finally block, or the process leaks one SQLite handle
per run until it runs out of file descriptors.

Heartbeats. A RUNNING row whose worker has been killed is indistinguishable from
one that is merely slow, unless the worker says so periodically. reap_stale()
uses that to mark abandoned work INTERRUPTED and to release the file.

The finally block. Whatever happens inside a task - a parse error, an
IllegalCharacterError from openpyxl on a control character in real client data,
a database lock, an AttributeError in a mapping rule - the job reaches a
terminal state and the file is put back into a status the user can act on. The
old convert endpoint caught only (EDIParseError, ValueError, OSError), so
anything else escaped as a 500 and left processing_status at CONVERTING with no
way back short of editing the database.
"""

from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from edi.models import ACTIVE_STATES, JobState, ProcessingJob

logger = logging.getLogger("edi.runner")

# How many jobs run at once. Small on purpose; see the module docstring.
WORKER_THREADS = getattr(settings, "EDI_WORKER_THREADS", 2)

# A RUNNING job whose heartbeat is older than this is treated as abandoned.
# Comfortably longer than HEARTBEAT_SECONDS so a busy worker is never reaped.
STALE_AFTER_SECONDS = getattr(settings, "EDI_JOB_STALE_SECONDS", 180)

# How often a running task refreshes its heartbeat and progress.
HEARTBEAT_SECONDS = getattr(settings, "EDI_JOB_HEARTBEAT_SECONDS", 5)

_executor = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, int(WORKER_THREADS)),
                    thread_name_prefix="edi-worker",
                )
                atexit.register(_shutdown)
    return _executor


def _shutdown():
    """Let in-flight work finish on a clean exit rather than truncating a workbook."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=True, cancel_futures=True)
        _executor = None


class JobProgress:
    """
    The handle a task uses to report on itself.

    Writes are throttled: a task that calls step() once per member loop on a
    forty thousand member file would otherwise issue forty thousand UPDATEs to
    report on work that takes three minutes. Only a percentage change or the
    heartbeat interval elapsing actually touches the database.
    """

    def __init__(self, job_id: int):
        self.job_id = job_id
        self._last_write = timezone.now()
        self._last_progress = -1

    def set(self, progress: int = None, message: str = None, force: bool = False):
        # Never report from inside an open transaction. The update would join
        # that transaction, so nobody polling would see it until the batch
        # committed - and worse, it would extend the write lock the batch is
        # already holding. Callers report at commit boundaries instead.
        from django.db import transaction as _transaction

        if _transaction.get_connection().in_atomic_block:
            return

        now = timezone.now()
        fields = {"heartbeat_at": now}

        if progress is not None:
            progress = max(0, min(99, int(progress)))
            fields["progress"] = progress

        if message is not None:
            fields["message"] = str(message)[:255]

        changed = progress is not None and progress != self._last_progress
        due = (now - self._last_write).total_seconds() >= HEARTBEAT_SECONDS

        if not (force or due or (changed and message is not None)):
            return

        try:
            ProcessingJob.objects.filter(pk=self.job_id).update(**fields)
        except Exception:  # noqa: BLE001 - progress reporting must never fail a job
            logger.debug("Could not write progress for job %s", self.job_id, exc_info=True)
            return

        self._last_write = now
        if progress is not None:
            self._last_progress = progress

    def fraction(self, done: int, total: int, floor: int = 0, ceiling: int = 99, message=None):
        """Report done/total scaled into a band, so two phases can share the bar."""
        if not total:
            return
        span = max(0, ceiling - floor)
        self.set(progress=floor + int(span * min(done, total) / total), message=message)


def enqueue(job: ProcessingJob, task) -> ProcessingJob:
    """
    Hand a queued job to the pool, or run it here if inline mode is on.

    task is called as task(job, progress). Everything about state transitions,
    connection hygiene and failure handling lives in _run rather than in the
    task, so no task can forget to release a file - and that holds whichever
    side of this branch it takes.
    """
    if getattr(settings, "EDI_RUN_JOBS_INLINE", False):
        _run(job.id, task, close_connections=False)
        job.refresh_from_db()
        return job

    _get_executor().submit(_run, job.id, task)
    return job


def _run(job_id: int, task, close_connections: bool = True):
    """
    The wrapper every background task runs inside.

    close_connections is False only for inline execution, where the connection
    belongs to the caller's thread and closing it would tear down the test
    transaction or the request's own connection mid-flight.
    """
    from edi.services.tasks import release_file  # local import; avoids a cycle

    job = None
    try:
        job = ProcessingJob.objects.filter(pk=job_id).first()
        if job is None:
            logger.warning("Job %s vanished before it started.", job_id)
            return

        if job.state not in ACTIVE_STATES:
            # Already reaped, cancelled or somehow run twice. Doing the work
            # again would produce a second workbook nobody asked for.
            logger.info("Job %s is %s; not running it.", job_id, job.state)
            return

        ProcessingJob.objects.filter(pk=job_id).update(
            state=JobState.RUNNING,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
            progress=1,
            message="Starting",
        )
        job.refresh_from_db()

        progress = JobProgress(job_id)
        result = task(job, progress) or {}

        ProcessingJob.objects.filter(pk=job_id).update(
            state=JobState.SUCCEEDED,
            progress=100,
            message=str(result.get("message", "Completed"))[:255],
            result=result,
            error="",
            finished_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )

    except Exception as exc:  # noqa: BLE001 - deliberately everything
        # Everything, because the point of this block is that no exception type
        # can leave a file stranded. A narrower except is how CONVERTING became
        # a state with no exit.
        logger.exception("Job %s failed", job_id)
        detail = "{kind}: {exc}".format(kind=type(exc).__name__, exc=exc)
        try:
            ProcessingJob.objects.filter(pk=job_id).update(
                state=JobState.FAILED,
                message=detail[:255],
                error=detail[:4000],
                finished_at=timezone.now(),
                heartbeat_at=timezone.now(),
            )
            if job is not None:
                release_file(job, detail)
        except Exception:  # noqa: BLE001
            logger.exception("Could not record the failure of job %s", job_id)

    finally:
        if close_connections:
            # A thread the ORM did not create keeps its connection open for
            # ever otherwise, and SQLite runs out of handles long before anyone
            # notices.
            close_old_connections()
            connections.close_all()


def reap_stale(include_queued: bool = False) -> int:
    """
    Release jobs whose worker is gone.

    Called at startup, where every active row is by definition orphaned - a
    fresh process has no threads from the last one - and periodically from the
    status endpoint, where only a missed heartbeat counts. Without this a
    container restart mid-conversion leaves the file at CONVERTING permanently,
    which is exactly the state the screenshot in the bug report shows.
    """
    from edi.services.tasks import release_file

    cutoff = timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS)
    queryset = ProcessingJob.objects.filter(state__in=ACTIVE_STATES)

    if not include_queued:
        queryset = queryset.filter(state=JobState.RUNNING).filter(
            heartbeat_at__isnull=True
        ) | ProcessingJob.objects.filter(
            state=JobState.RUNNING, heartbeat_at__lt=cutoff
        )
        queryset = queryset.filter(started_at__lt=cutoff)

    reaped = 0
    for job in queryset.select_related("uploaded_file"):
        note = (
            "The worker running this job stopped before it finished. "
            "Nothing was lost; run it again."
        )
        ProcessingJob.objects.filter(pk=job.pk).update(
            state=JobState.INTERRUPTED,
            message="Interrupted",
            error=note,
            finished_at=timezone.now(),
        )
        try:
            release_file(job, note)
        except Exception:  # noqa: BLE001
            logger.exception("Could not release the file behind job %s", job.pk)
        reaped += 1

    if reaped:
        logger.warning("Released %d interrupted job(s).", reaped)
    return reaped

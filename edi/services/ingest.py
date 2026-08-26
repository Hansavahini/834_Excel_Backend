"""
Drive the members tables from a stored 834.

Two passes, deliberately.

An 834 does not promise that a subscriber loop precedes its dependents. Change
files routinely carry a dependent on its own, and even in a full file a sponsor
can order the INS loops however its extract happens to emit them. The previous
single pass handled that by rewriting the dependent's member_type to SUB so the
check constraint would let the row save, and nothing ever changed it back: the
relink logic only looked at DEP rows, so the "temporary" subscriber was
permanent and the family structure was quietly wrong from then on.

So: pass one creates and updates every subscriber and remembers which loop each
one came from. Pass two walks the same file again and attaches each dependent to
the subscriber that was in scope for it. A dependent whose subscriber genuinely
is not in the file stays a DEP with subscriber_pending set, and the relink sweep
at the end joins it up as soon as the subscriber arrives in any later file.

The file is read twice rather than held in memory once, which is the right trade
for a 40,000 member interchange - and it is a cheap trade besides. Measured on a
sixty thousand segment file, the whole parse takes 0.18 seconds; the database is
four hundred times more expensive than the reader, so the second pass is not
where the time goes and never was.

Where the time did go, and what happens now.

The old run issued twenty-five to thirty-one queries per loop and committed
every two hundred and fifty of them. Three changes moved the cost:

  RosterIndex loads the client's existing roster in three queries at the start
  and answers every identity question from memory.

  A loop whose content digest matches what last wrote that person is skipped
  outright, which is what turns a repeated daily roster from a full rewrite into
  a couple of bulk inserts. It is also the honest form of "do not store a member
  who already exists": the write path is not merely idempotent, it does not run.

  Presence, enrollment and change rows are buffered in the SyncContext and
  written in bulk at batch boundaries.

The batching context around the two passes stays, for the reason it was
introduced: sync_member_loop is atomic, so without an outer transaction every
loop is its own commit and on SQLite every commit is an fsync.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from members.models import CustodialParent

from .loop_extractor import StreamingParsedFile
from .member_sync import SyncContext, relink_pending_dependents, sync_member_loop
from .parser import EDI834Parser
from .roster_index import RosterIndex
from .roster_sync import relink_dependants
from .x12_834_to_db import convert_834_to_member, custodial_parent_from

logger = logging.getLogger("edi.ingest")


def sync_custodial_parent(member, loop):
    """
    Write the S3 block when the file carries one. Absent on most loops.

    Skipped entirely on the fast path: a member whose digest is unchanged has an
    unchanged custodial parent block too, and this used to be one guaranteed
    query per loop looking for a segment the overwhelming majority of loops do
    not contain. The check is now on the parsed loop, in memory, before the
    database is consulted at all.
    """
    if getattr(member, "is_light", False):
        return
    details = custodial_parent_from(loop)
    if not details or not (details.get("last_name") or details.get("first_name")):
        return
    CustodialParent.objects.update_or_create(member=member, defaults=details)


# How many member loops share one database transaction.
#
# sync_member_loop is decorated @transaction.atomic, so with nothing around it
# every loop was its own commit - and on SQLite a commit is an fsync. A 12,000
# loop file therefore paid roughly 24,000 fsyncs across the two passes, which
# measured at 173 seconds of wall clock for work that is only a few seconds of
# actual database time. Batching turns the inner atomic into a savepoint, which
# costs almost nothing, and commits once per batch instead.
#
# 500 rather than "the whole file" because a batch is also the rollback unit if
# the process dies mid-run, and because a transaction held open for three
# minutes blocks every writer behind it. It is double the old 250 because the
# per-loop work is now much smaller, so a batch covers the same wall clock.
SYNC_BATCH_SIZE = 500


@contextmanager
def _batched(size=SYNC_BATCH_SIZE, on_commit=None, context=None):
    """
    Yield a function that opens a transaction and commits it every `size` calls.

    The per-loop @transaction.atomic inside sync_member_loop becomes a savepoint
    while one of these is open, so a single malformed loop still rolls back
    alone and the rest of the batch survives - the behaviour the old code got
    from having no outer transaction at all, at a fraction of the cost.

    context, when given, is flushed inside the transaction just before it
    commits, so the buffered presence and enrollment rows land in the same unit
    of work as the member rows they describe.
    """
    state = {"atomic": None, "count": 0}

    def close():
        if state["atomic"] is not None:
            try:
                if context is not None:
                    context.flush()
            finally:
                state["atomic"].__exit__(None, None, None)
                state["atomic"] = None
                state["count"] = 0
            if on_commit is not None:
                # Progress is reported here and only here. Reporting from inside
                # the batch would put the update in the same transaction, so no
                # poller could see it until the batch committed anyway.
                on_commit()

    def tick():
        if state["atomic"] is None:
            state["atomic"] = transaction.atomic()
            state["atomic"].__enter__()
        state["count"] += 1
        if state["count"] >= size:
            close()

    try:
        yield tick
    finally:
        close()


def _new_summary():
    return {
        "loops": 0,
        "synced": 0,
        "failed": 0,
        "added": 0,
        "changed": 0,
        "terminated": 0,
        "reinstated": 0,
        "unchanged": 0,
        "relinked": 0,
        "dependants_relinked": 0,
        # New: how much of the file the fast path absorbed, and how many change
        # events the monitor raised. Both are worth surfacing - "6,000 loops,
        # 5,880 unchanged, 143 changes recorded" is a far more useful line in a
        # log or on a screen than "6,000 synced".
        "skipped_unchanged": 0,
        "changes_recorded": 0,
        "errors": [],
    }


def _record_error(summary, loop_id, exc):
    summary["failed"] += 1
    message = "loop {loop}: {kind}: {exc}".format(
        loop=loop_id, kind=type(exc).__name__, exc=exc
    )
    if len(summary["errors"]) < 50:
        summary["errors"].append(message)
    logger.error("Member sync failed on %s", message)


def sync_uploaded_file(record, owner, client=None, on_progress=None):
    """
    Parse the stored file and reconcile every member loop.

    Returns a summary dict. Loop level failures are counted and logged rather
    than aborting the run, because one malformed INS loop in a 40,000 member
    file must not cost the other 39,999.

    on_progress, when given, is called as on_progress(loops_done, phase) so the
    background runner can report something more useful than a spinner. It is
    called at commit boundaries and is expected to throttle itself; JobProgress
    does.
    """
    summary = _new_summary()
    if client is None:
        client = getattr(record, "client", None)

    status_date = record.file_date or timezone.now().date()
    path = record.stored_file.path
    done = 0

    phase = {"name": "Syncing"}

    def tell(name=None):
        if name:
            phase["name"] = name
        if on_progress is None:
            return
        try:
            on_progress(done, phase["name"])
        except Exception:  # noqa: BLE001 - reporting must never fail the sync
            logger.debug("Progress callback failed", exc_info=True)

    tell("Loading roster")
    index = RosterIndex.load(owner, client)

    context = SyncContext(
        owner=owner,
        client=client,
        source_file=record,
        status_date=status_date,
        index=index,
    )

    # ---------------------------------------------------------------
    # Pass 1 - subscribers only. Remember which loop each one owns so
    # pass 2 can work out which subscriber was in scope for a dependent
    # without holding the whole file.
    # ---------------------------------------------------------------
    subscriber_by_loop = {}
    first_pass = StreamingParsedFile(EDI834Parser(path).iter_segments())

    phase["name"] = "Syncing subscribers"
    with _batched(on_commit=tell, context=context) as commit_point:
        for loop in first_pass:
            summary["loops"] += 1
            done += 1
            if not bool(getattr(loop, "is_subscriber", True)):
                continue
            try:
                commit_point()
                parsed_dict = convert_834_to_member(loop)
                member, change_type, _ = sync_member_loop(
                    parsed_dict=parsed_dict,
                    owner=owner,
                    source_file=record,
                    status_date=status_date,
                    current_subscriber=None,
                    client=client,
                    context=context,
                )
                sync_custodial_parent(member, loop)
                subscriber_by_loop[loop.loop_id] = member
                summary["synced"] += 1
                key = str(change_type).lower()
                if key in summary:
                    summary[key] += 1
            except Exception as exc:  # noqa: BLE001 - one loop must not stop the file
                _record_error(summary, getattr(loop, "loop_id", summary["loops"]), exc)

    # ---------------------------------------------------------------
    # Pass 2 - dependents, attached to the subscriber pass 1 created.
    # ---------------------------------------------------------------
    second_pass = StreamingParsedFile(EDI834Parser(path).iter_segments())
    current_subscriber = None

    phase["name"] = "Syncing dependants"
    with _batched(on_commit=tell, context=context) as commit_point:
        for loop in second_pass:
            done += 1
            if bool(getattr(loop, "is_subscriber", True)):
                # A subscriber loop that failed in pass 1 is absent from the
                # map, which correctly clears the scope: better a pending
                # dependent than one attached to the previous family.
                current_subscriber = subscriber_by_loop.get(loop.loop_id)
                continue

            try:
                commit_point()
                parsed_dict = convert_834_to_member(loop)
                member, change_type, _ = sync_member_loop(
                    parsed_dict=parsed_dict,
                    owner=owner,
                    source_file=record,
                    status_date=status_date,
                    current_subscriber=current_subscriber,
                    client=client,
                    context=context,
                )
                sync_custodial_parent(member, loop)
                summary["synced"] += 1
                key = str(change_type).lower()
                if key in summary:
                    summary[key] += 1
            except Exception as exc:  # noqa: BLE001
                _record_error(summary, getattr(loop, "loop_id", "?"), exc)

    # Anything still buffered when the last batch closed short of its size.
    with transaction.atomic():
        context.flush()

    tell("Relinking families")
    summary["relinked"] = relink_pending_dependents(owner, client)
    # The master tables get the same treatment, and after the Member side so the
    # subscriber link it reads from is already correct.
    summary["dependants_relinked"] = relink_dependants(owner, client)

    summary["skipped_unchanged"] = context.counters["skipped"]
    summary["changes_recorded"] = context.counters["events"]

    logger.info(
        "Synced %s of %s loops from %s (%s unchanged and skipped, %s changes recorded, "
        "%s failed, %s relinked)",
        summary["synced"],
        summary["loops"],
        record.original_filename,
        summary["skipped_unchanged"],
        summary["changes_recorded"],
        summary["failed"],
        summary["relinked"],
    )
    return summary

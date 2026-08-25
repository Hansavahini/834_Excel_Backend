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
for a 40,000 member interchange.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from members.models import CustodialParent

from .loop_extractor import StreamingParsedFile
from .member_sync import relink_pending_dependents, sync_member_loop
from .parser import EDI834Parser
from .x12_834_to_db import convert_834_to_member, custodial_parent_from

logger = logging.getLogger("edi.ingest")


def sync_custodial_parent(member, loop):
    """Write the S3 block when the file carries one. Absent on most loops."""
    details = custodial_parent_from(loop)
    if not details or not (details.get("last_name") or details.get("first_name")):
        return
    CustodialParent.objects.update_or_create(member=member, defaults=details)


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


def sync_uploaded_file(record, owner, client=None):
    """
    Parse the stored file and reconcile every member loop.

    Returns a summary dict. Loop level failures are counted and logged rather
    than aborting the run, because one malformed INS loop in a 40,000 member
    file must not cost the other 39,999.
    """
    summary = _new_summary()
    if client is None:
        client = getattr(record, "client", None)

    status_date = record.file_date or timezone.now().date()
    path = record.stored_file.path

    # ---------------------------------------------------------------
    # Pass 1 — subscribers only. Remember which loop each one owns so
    # pass 2 can work out which subscriber was in scope for a dependent
    # without holding the whole file.
    # ---------------------------------------------------------------
    subscriber_by_loop = {}
    first_pass = StreamingParsedFile(EDI834Parser(path).iter_segments())

    for loop in first_pass:
        summary["loops"] += 1
        if not bool(getattr(loop, "is_subscriber", True)):
            continue
        try:
            parsed_dict = convert_834_to_member(loop)
            member, change_type, _ = sync_member_loop(
                parsed_dict=parsed_dict,
                owner=owner,
                source_file=record,
                status_date=status_date,
                current_subscriber=None,
                client=client,
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
    # Pass 2 — dependents, attached to the subscriber pass 1 created.
    # ---------------------------------------------------------------
    second_pass = StreamingParsedFile(EDI834Parser(path).iter_segments())
    current_subscriber = None

    for loop in second_pass:
        if bool(getattr(loop, "is_subscriber", True)):
            # A subscriber loop that failed in pass 1 is absent from the map,
            # which correctly clears the scope: better a pending dependent than
            # one attached to the previous family.
            current_subscriber = subscriber_by_loop.get(loop.loop_id)
            continue

        try:
            parsed_dict = convert_834_to_member(loop)
            member, change_type, _ = sync_member_loop(
                parsed_dict=parsed_dict,
                owner=owner,
                source_file=record,
                status_date=status_date,
                current_subscriber=current_subscriber,
                client=client,
            )
            sync_custodial_parent(member, loop)
            summary["synced"] += 1
            key = str(change_type).lower()
            if key in summary:
                summary[key] += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(summary, getattr(loop, "loop_id", "?"), exc)

    summary["relinked"] = relink_pending_dependents(owner, client)

    logger.info(
        "Synced %s of %s loops from %s (%s failed, %s relinked)",
        summary["synced"],
        summary["loops"],
        record.original_filename,
        summary["failed"],
        summary["relinked"],
    )
    return summary

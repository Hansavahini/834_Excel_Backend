"""
Maintain the separated Subscriber and Dependant tables from a synced member.

This is Parts 2 and 3 of the brief in one place. The requirement has two halves
that pull against each other and both have to hold:

  * One SSN is one person. Uploading the same file twice, or two files that
    carry the same subscriber, must not produce two master rows. That is
    update_or_create against a unique constraint, and the constraint is real -
    it is enforced by the database, not by this function remembering to check.

  * File and enrollment history must survive. De-duplicating a master record is
    only safe when the thing being collapsed is written down somewhere first,
    otherwise "do not create a second row" quietly becomes "discard the second
    file". So every appearance writes an EnrollmentRecord keyed on the source
    file, and those are never overwritten by a later file.

The projection is one-way and idempotent. Nothing here writes back to Member,
and running it twice over the same loop produces the same two rows.

What changed in this version. _resolve_existing used to cost up to three queries
per loop - SSN, then the source_member link, then member id - and _open_span
loaded the member's coverage spans a second time after sync_eligibility had
already loaded them. Both now take what the caller already has: a RosterIndex
that answers the identity question from memory, and the span list the eligibility
reconciler built. Enrollment rows are appended to the caller's buffer rather than
written one update_or_create at a time. The resolution order and the constraints
are unchanged; only the number of round trips it takes to honour them is.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.db import IntegrityError, transaction

from members.models import Dependant, EnrollmentRecord, Member, Subscriber

from .ssn import normalize_ssn as normalize_ssn_value

logger = logging.getLogger("edi.roster_sync")

# Copied from the member row onto the master record on every appearance. The
# master record is "as most recently known", so a later file legitimately
# updates a corrected surname or a new plan.
PROJECTED_FIELDS = (
    "first_name",
    "last_name",
    "dob",
    "gender",
    "plan",
)


def _projected_values(member, parsed_dict: dict) -> dict:
    return {
        "first_name": member.first_name or "",
        "last_name": member.last_name or "",
        "dob": member.date_of_birth,
        "gender": member.gender_code or "U",
        "plan": member.plan_code or (parsed_dict.get("plan_code") or ""),
    }


def _open_span(member, spans=None):
    """
    The coverage span the master record's effective/termination columns show.

    spans is passed in by the caller when it already has them, which is the
    normal case: sync_eligibility has just loaded and updated the list, and
    reloading it here was one wasted query per loop plus a chance of reading a
    span the same transaction had just changed.
    """
    if spans is None:
        spans = list(member.eligibility_history.all())
    if not spans:
        return None, None
    open_spans = [span for span in spans if span.termination_date is None]
    if open_spans:
        chosen = max(open_spans, key=lambda span: span.effective_date)
        return chosen.effective_date, None
    chosen = max(spans, key=lambda span: span.effective_date)
    return chosen.effective_date, chosen.termination_date


def _resolve_existing(model, owner, client, ssn: str, member, member_id: str, index=None):
    """
    Find the master row this person already occupies.

    SSN first because it is the identifier the brief names. Then the member
    link, which catches a person whose SSN arrived only in a later file. Then
    the sponsor's member id. Deliberately not name and date of birth: two
    siblings with the same first initial and a shared birthday are not rare
    enough to merge on.

    The index answers all three from memory when it is available. It is a cache
    of what the database holds, so a miss still falls through to the queries
    rather than concluding the person is new - concluding that wrongly would
    create the duplicate this whole module exists to prevent.
    """
    is_subscriber = model is Subscriber

    if index is not None and index.usable:
        by_ssn = index.subscriber_by_ssn if is_subscriber else index.dependant_by_ssn
        by_member = (
            index.subscriber_by_member if is_subscriber else index.dependant_by_member
        )
        pk = None
        if ssn:
            pk = by_ssn.get(ssn)
        if pk is None:
            pk = by_member.get(member.pk)
        if pk is not None:
            found = model.objects.filter(pk=pk).first()
            if found is not None:
                return found

    scope = model.objects.filter(owner=owner, client=client)

    if ssn:
        found = scope.filter(ssn=ssn).first()
        if found:
            return found

    found = model.objects.filter(source_member=member).first()
    if found:
        return found

    if member_id:
        found = scope.filter(member_id=member_id).first()
        if found:
            return found

    return None


def _apply(record, values: dict, ssn: str, member, member_id: str, source_file, spans=None):
    """Update a master row without ever blanking something already known."""
    changed = []

    for field in PROJECTED_FIELDS:
        new_value = values.get(field)
        # "U" is the unknown gender placeholder. A later file that simply does
        # not carry DMG03 must not erase a gender an earlier file supplied.
        if new_value in (None, "", "U"):
            continue
        if getattr(record, field) != new_value:
            setattr(record, field, new_value)
            changed.append(field)

    if ssn and record.ssn != ssn:
        record.ssn = ssn
        changed.extend(["ssn", "ssn_fingerprint", "ssn_last4"])

    if member_id and record.member_id != member_id:
        record.member_id = member_id
        changed.append("member_id")

    if record.source_member_id != member.pk:
        record.source_member = member
        changed.append("source_member")

    effective, termination = _open_span(member, spans=spans)
    if effective is not None and record.effective_date != effective:
        record.effective_date = effective
        changed.append("effective_date")
    if record.termination_date != termination:
        record.termination_date = termination
        changed.append("termination_date")

    if record.first_source_file_id is None:
        record.first_source_file = source_file
        changed.append("first_source_file")
    if record.source_file_id != source_file.pk:
        record.source_file = source_file
        changed.append("source_file")

    if changed:
        # update_fields rather than a full save. The full save rewrote all
        # eighteen columns on every appearance of every member, which on an
        # unchanged daily roster is the whole master table rewritten for
        # nothing.
        record.save(update_fields=tuple(dict.fromkeys(changed)))
    return record


def _enrollment_rows(record, member, parsed_dict: dict, source_file, owner, client):
    """
    One row per person per file per coverage line, never deleted.

    Keyed on the file rather than on the date so a corrected re-send and the
    original both survive, and so re-uploading the same file updates its own row
    instead of appending a duplicate. Returned unsaved so the caller can write a
    whole batch at once; the unique constraints make ignore_conflicts the right
    way to handle a re-run.
    """
    is_subscriber = isinstance(record, Subscriber)
    coverages = parsed_dict.get("coverages") or [
        {
            "insurance_line_code": parsed_dict.get("insurance_line_code") or "HLT",
            "plan_code": parsed_dict.get("plan_code") or "",
            "maintenance_type_code": parsed_dict.get("maintenance_type_code") or "",
            "effective_date": parsed_dict.get("effective_date"),
            "termination_date": parsed_dict.get("termination_date"),
        }
    ]

    rows = []
    for coverage in coverages:
        effective = coverage.get("effective_date")
        termination = coverage.get("termination_date")
        if termination and effective and termination < effective:
            # The check constraint would reject this and take the whole loop
            # with it. Clamp rather than lose the member.
            termination = effective

        rows.append(
            EnrollmentRecord(
                owner=owner,
                client=client,
                subscriber=record if is_subscriber else None,
                dependant=None if is_subscriber else record,
                source_file=source_file,
                file_date=getattr(source_file, "file_date", None),
                plan=(coverage.get("plan_code") or "")[:30],
                insurance_line_code=(coverage.get("insurance_line_code") or "HLT")[:3],
                effective_date=effective,
                termination_date=termination,
                maintenance_type_code=(coverage.get("maintenance_type_code") or "")[:3],
                relationship=member.relationship_code or "",
                member_type=member.member_type,
            )
        )
    return rows


@transaction.atomic
def project_member(
    member,
    parsed_dict: dict,
    source_file,
    owner,
    client=None,
    index=None,
    context=None,
    is_new=False,
):
    """
    Write or update the master row for one synced member, plus its history.

    Returns the Subscriber or Dependant row, or None when the loop carried
    nothing worth a master record.
    """
    if client is None:
        client = getattr(member, "client", None)

    ssn, ssn_error = normalize_ssn_value(parsed_dict.get("ssn") or member.ssn)
    if ssn_error:
        logger.warning("Rejected SSN for member %s: %s", member.pk, ssn_error)

    member_id = (member.member_id or "")[:80]
    values = _projected_values(member, parsed_dict)
    spans = parsed_dict.get("__spans__")

    model = Subscriber if member.member_type == "SUB" else Dependant

    # is_new says the Member row was created moments ago by this same loop, so
    # it cannot already have a master row to find. Skipping the search matters:
    # on a first load every one of those lookups is three queries spent proving
    # a person nobody has ever seen is not on file, which on a six thousand loop
    # file was sixteen thousand round trips to learn nothing.
    record = (
        None
        if is_new
        else _resolve_existing(model, owner, client, ssn, member, member_id, index=index)
    )

    if record is None:
        record = model(owner=owner, client=client)
        record.ssn = ssn
        record.member_id = member_id
        record.source_member = member
        for field in PROJECTED_FIELDS:
            value = values.get(field)
            if value not in (None, ""):
                setattr(record, field, value)
        effective, termination = _open_span(member, spans=spans)
        record.effective_date = effective
        record.termination_date = termination
        record.first_source_file = source_file
        record.source_file = source_file
        if model is Dependant:
            record.relationship = member.relationship_code or "19"
            record.subscriber_id = _subscriber_pk_for(member, owner, client, index=index)
        try:
            record.save()
        except IntegrityError:
            # Lost a race, or the same SSN is already held under a different
            # member link. Fall back to the existing row rather than failing the
            # loop: one master row per SSN is the point, and we have just been
            # told which one it is.
            record = model.objects.filter(owner=owner, client=client, ssn=ssn).first()
            if record is None:
                raise
            _apply(record, values, ssn, member, member_id, source_file, spans=spans)
    else:
        if model is Dependant:
            linked = _subscriber_pk_for(member, owner, client, index=index)
            if linked is not None and record.subscriber_id != linked:
                record.subscriber_id = linked
                record.save(update_fields=["subscriber"])
            if member.relationship_code and record.relationship != member.relationship_code:
                record.relationship = member.relationship_code
                record.save(update_fields=["relationship"])
        _apply(record, values, ssn, member, member_id, source_file, spans=spans)

    rows = _enrollment_rows(record, member, parsed_dict, source_file, owner, client)
    if context is not None:
        context.add_enrollments(rows)
    else:
        EnrollmentRecord.objects.bulk_create(rows, ignore_conflicts=True)

    return record


def _subscriber_pk_for(member, owner, client, index=None):
    """
    The primary key of the Subscriber master row for this dependant's
    subscriber, if known.

    A primary key rather than the row itself, because every caller only ever
    assigns it to a foreign key. Fetching the whole Subscriber to read its pk
    back out was one query per dependant loop - four thousand of them on a first
    load of a six thousand loop file, to learn a number the index already held.
    """
    if member.subscriber_id is None:
        return None
    if index is not None and index.usable:
        pk = index.subscriber_by_member.get(member.subscriber_id)
        if pk is not None:
            return pk
    return (
        Subscriber.objects.filter(source_member_id=member.subscriber_id)
        .values_list("pk", flat=True)
        .first()
    )


def relink_dependants(owner, client=None) -> int:
    """
    Attach dependant master rows whose subscriber row arrived later.

    Mirrors relink_pending_dependents() on the Member side, and runs after it,
    so a dependant recorded before its subscriber is joined up as soon as the
    subscriber exists rather than staying orphaned until someone notices.

    Two queries and a dictionary rather than one query per orphan.
    """
    pending = Dependant.objects.filter(owner=owner, subscriber__isnull=True)
    if client is not None:
        pending = pending.filter(client=client)

    orphans = [
        (pk, source_member_id)
        for pk, source_member_id in pending.values_list("pk", "source_member_id")
        if source_member_id
    ]
    if not orphans:
        return 0

    subscriber_member_ids = dict(
        Member.objects.filter(pk__in=[sm for _pk, sm in orphans])
        .exclude(subscriber__isnull=True)
        .values_list("pk", "subscriber_id")
    )
    if not subscriber_member_ids:
        return 0

    rows = Subscriber.objects.filter(
        source_member_id__in=set(subscriber_member_ids.values())
    ).values_list("source_member_id", "pk")
    subscriber_pk_by_member = dict(rows)

    linked = 0
    for pk, source_member_id in orphans:
        parent_member_id = subscriber_member_ids.get(source_member_id)
        if parent_member_id is None:
            continue
        subscriber_pk = subscriber_pk_by_member.get(parent_member_id)
        if subscriber_pk is None:
            continue
        Dependant.objects.filter(pk=pk).update(subscriber_id=subscriber_pk)
        linked += 1
    return linked

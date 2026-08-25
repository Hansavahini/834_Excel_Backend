"""
Write one parsed 834 member loop into the members tables.

What changed from the previous version and why:

  * Creating a member wrote nine of the twenty-odd fields the parser had
    already extracted. Address, city, state, postal code, phone, email,
    subscriber number, group number, union local, class code and the three INS
    status codes were parsed and then thrown away, so the database could never
    answer a question the Excel export could. Every parsed field is written now,
    on create and on update.

  * sync_eligibility called objects.create() on a table with a unique
    constraint over (member, insurance_line_code, effective_date). Re-uploading
    a file, or any member whose coverage restarted on a date already recorded,
    raised IntegrityError inside the surrounding atomic block, which rolled back
    the whole loop and lost the member. It is a get_or_create now.

  * plan_code was written straight from the parsed dict, which is None when the
    loop has no HD segment. The column is NOT NULL, so that was a second
    IntegrityError waiting on the same code path.

  * A dependent whose subscriber loop failed to parse violated the
    dependent_requires_subscriber check constraint and was dropped silently.
    It is now recorded as a standalone member and flagged, which is recoverable.

  * Termination was recognised only from INS03=024. Files that close a span
    with DTP*349 and a 001 change code left the span open forever, so those
    members read as covered indefinitely — the exact failure the Info section
    would have surfaced as a wrong presence flag.

  * coverage_status was set ad hoc in two branches and left stale in the rest.
    It is derived from the spans in one place at the end of the sync.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone

from members.models import (
    CoverageStatus,
    Member,
    MemberDailyStatus,
    MemberEligibilityHistory,
)

from .identity import resolve_member_identity

logger = logging.getLogger("edi.member_sync")

TERMINATION_CODE = "024"
REINSTATEMENT_CODE = "025"

# Written on create and compared on update. member_id is handled separately
# because it must never be blanked once known.
DEMOGRAPHIC_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
    "name_suffix",
    "gender_code",
    "date_of_birth",
    "address1",
    "address2",
    "city",
    "state",
    "postal_code",
    "phone",
    "email",
    "plan_code",
    "class_code",
    "subscriber_number",
    "group_number",
    "local",
    "benefit_status_code",
    "employment_status_code",
    "student_status_code",
)

# Changes to these are worth showing an operator in the Info and Member Search
# screens. A postal code correction is real but it is noise on a change report.
NOTABLE_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
    "gender_code",
    "date_of_birth",
    "address1",
    "city",
    "state",
    "postal_code",
    "plan_code",
    "class_code",
    "group_number",
)


def _clean(value, default=""):
    """The models use blank strings, not nulls, for every optional CharField."""
    return default if value is None else value


def get_changed_fields(member: Member, parsed_dict: dict) -> dict:
    """Field level diff between what is stored and what the file says."""
    changed = {}
    for field in NOTABLE_FIELDS:
        new_val = parsed_dict.get(field)
        if new_val in (None, ""):
            continue
        old_val = getattr(member, field)
        if old_val == new_val:
            continue
        changed[field] = [
            old_val.isoformat() if hasattr(old_val, "isoformat") else old_val,
            new_val.isoformat() if hasattr(new_val, "isoformat") else new_val,
        ]
    return changed


def apply_demographics(member: Member, parsed_dict: dict) -> None:
    """Copy every parsed field onto the member, never blanking a known value."""
    for field in DEMOGRAPHIC_FIELDS:
        new_val = parsed_dict.get(field)
        if new_val in (None, ""):
            continue
        setattr(member, field, new_val)

    if parsed_dict.get("member_id") and not member.member_id:
        member.member_id = parsed_dict["member_id"]
    if parsed_dict.get("ssn") and not member.ssn:
        member.ssn = parsed_dict["ssn"]
    if parsed_dict.get("relationship_code"):
        member.relationship_code = parsed_dict["relationship_code"]

    member.save()


def derive_coverage_status(member: Member, as_of=None) -> str:
    """
    One place that decides ACTIVE versus TERMINATED, read from the spans.

    Anything with an open span, or a span that has not yet closed as of the
    date being processed, is active. Everything else with at least one span is
    terminated. No spans at all stays UNKNOWN rather than being guessed.
    """
    as_of = as_of or timezone.now().date()
    spans = list(member.eligibility_history.all())
    if not spans:
        return CoverageStatus.UNKNOWN
    for span in spans:
        if span.termination_date is None or span.termination_date >= as_of:
            return CoverageStatus.ACTIVE
    return CoverageStatus.TERMINATED


def sync_eligibility(member: Member, parsed_dict: dict, source_file, status_date) -> str:
    """
    Reconcile every coverage line in the loop against the stored spans.

    Returns the strongest action taken, ordered TERMINATED > REINSTATED >
    CHANGED > ADDED > UNCHANGED, which is what the daily status row records.
    """
    coverages = parsed_dict.get("coverages") or [
        {
            "insurance_line_code": parsed_dict.get("insurance_line_code") or "HLT",
            "plan_code": parsed_dict.get("plan_code") or "",
            "maintenance_type_code": parsed_dict.get("maintenance_type_code") or "",
            "effective_date": parsed_dict.get("effective_date"),
            "termination_date": parsed_dict.get("termination_date"),
        }
    ]

    ranking = {"UNCHANGED": 0, "ADDED": 1, "CHANGED": 2, "REINSTATED": 3, "TERMINATED": 4}
    outcome = "UNCHANGED"

    def record(action):
        nonlocal outcome
        if ranking[action] > ranking[outcome]:
            outcome = action

    for coverage in coverages:
        line = (coverage.get("insurance_line_code") or "HLT")[:3]
        plan = _clean(coverage.get("plan_code"))[:30]
        mtc = _clean(coverage.get("maintenance_type_code"))[:3]
        effective = coverage.get("effective_date")
        termination = coverage.get("termination_date")

        # A termination is either an explicit 024 or an end date on the line.
        is_terminating = mtc == TERMINATION_CODE or termination is not None

        open_span = (
            MemberEligibilityHistory.objects.filter(
                member=member, insurance_line_code=line, termination_date__isnull=True
            )
            .order_by("-effective_date")
            .first()
        )

        if is_terminating:
            end_date = termination or status_date
            if open_span:
                # A span cannot end before it started; the constraint would
                # reject it and the whole loop would be lost.
                if end_date < open_span.effective_date:
                    end_date = open_span.effective_date
                open_span.termination_date = end_date
                if mtc:
                    open_span.maintenance_type_code = mtc
                open_span.save(
                    update_fields=["termination_date", "maintenance_type_code"]
                )
                record("TERMINATED")
            elif effective:
                # Terminating a span nobody recorded. Write the closed span so
                # the history is complete rather than dropping the fact.
                span, created = MemberEligibilityHistory.objects.get_or_create(
                    member=member,
                    insurance_line_code=line,
                    effective_date=effective,
                    defaults={
                        "plan_code": plan,
                        "class_code": _clean(parsed_dict.get("class_code"))[:30],
                        "termination_date": max(end_date, effective),
                        "maintenance_type_code": mtc,
                        "source_file": source_file,
                    },
                )
                if not created and span.termination_date is None:
                    span.termination_date = max(end_date, span.effective_date)
                    span.save(update_fields=["termination_date"])
                record("TERMINATED")
            continue

        start = effective or status_date

        if open_span is None:
            span, created = MemberEligibilityHistory.objects.get_or_create(
                member=member,
                insurance_line_code=line,
                effective_date=start,
                defaults={
                    "plan_code": plan,
                    "class_code": _clean(parsed_dict.get("class_code"))[:30],
                    "maintenance_type_code": mtc,
                    "source_file": source_file,
                },
            )
            if created:
                record("REINSTATED" if mtc == REINSTATEMENT_CODE else "ADDED")
            elif span.termination_date is not None:
                # A file reopening a span that was previously closed.
                span.termination_date = None
                span.maintenance_type_code = mtc or span.maintenance_type_code
                span.save(update_fields=["termination_date", "maintenance_type_code"])
                record("REINSTATED")
            continue

        # An open span already exists for this line.
        if plan and open_span.plan_code != plan:
            close_on = (
                start if start > open_span.effective_date else open_span.effective_date
            )
            open_span.termination_date = close_on
            open_span.save(update_fields=["termination_date"])

            span, created = MemberEligibilityHistory.objects.get_or_create(
                member=member,
                insurance_line_code=line,
                effective_date=start,
                defaults={
                    "plan_code": plan,
                    "class_code": _clean(parsed_dict.get("class_code"))[:30],
                    "maintenance_type_code": mtc,
                    "source_file": source_file,
                },
            )
            if not created:
                span.plan_code = plan
                span.termination_date = None
                span.save(update_fields=["plan_code", "termination_date"])
            record("CHANGED")

    return outcome


@transaction.atomic
def sync_member_loop(
    parsed_dict: dict,
    owner,
    source_file,
    status_date,
    current_subscriber: Optional[Member] = None,
    client=None,
) -> Tuple[Member, str, dict]:
    """
    Main entry point, called once per INS loop.

    Returns (member, change_type, changed_fields).
    """
    if client is None:
        client = getattr(source_file, "client", None)

    member = resolve_member_identity(parsed_dict, owner, current_subscriber, client=client)

    member_type = parsed_dict.get("member_type") or "SUB"
    subscriber_pending = False
    if member_type == "DEP" and current_subscriber is None:
        # The old code rewrote member_type to SUB here so the check constraint
        # would accept the row. That is a lie the system never took back: the
        # relink pass only looks at DEP rows, so a dependent promoted to
        # subscriber stayed a subscriber forever and its family tree was wrong
        # from then on. The type is now left alone and the row is flagged
        # instead, which the relink pass below can act on.
        subscriber_pending = True
        logger.warning(
            "Dependent %s %s arrived with no subscriber in scope; flagged pending linkage.",
            parsed_dict.get("first_name"),
            parsed_dict.get("last_name"),
        )

    change_type = MemberDailyStatus.ChangeType.UNCHANGED
    changed_fields: dict = {}

    if member is None:
        member = Member(
            owner=owner,
            client=client,
            member_type=member_type,
            subscriber=current_subscriber if member_type == "DEP" else None,
            subscriber_pending=subscriber_pending,
            relationship_code=parsed_dict.get("relationship_code") or "18",
            member_id=_clean(parsed_dict.get("member_id"))[:80],
            ssn=_clean(parsed_dict.get("ssn")),
            first_seen_file=source_file,
            last_seen_file=source_file,
        )
        for field in DEMOGRAPHIC_FIELDS:
            value = parsed_dict.get(field)
            if value not in (None, ""):
                setattr(member, field, value)
        member.save()
        change_type = MemberDailyStatus.ChangeType.ADDED
    else:
        changed_fields = get_changed_fields(member, parsed_dict)
        apply_demographics(member, parsed_dict)
        if changed_fields:
            change_type = MemberDailyStatus.ChangeType.CHANGED

        # A dependent that first arrived without its subscriber gets relinked
        # as soon as the subscriber is known, and stops being pending.
        if (
            member.member_type == "DEP"
            and member.subscriber_id is None
            and current_subscriber is not None
            and current_subscriber.pk != member.pk
        ):
            member.subscriber = current_subscriber
            member.subscriber_pending = False
            member.save(update_fields=["subscriber", "subscriber_pending"])
        elif member.member_type == "DEP" and member.subscriber_id is None:
            member.subscriber_pending = True
            member.save(update_fields=["subscriber_pending"])

        if client is not None and member.client_id is None:
            member.client = client
            member.save(update_fields=["client"])

        member.last_seen_file = source_file
        member.save(update_fields=["last_seen_file"])

    eligibility_action = sync_eligibility(member, parsed_dict, source_file, status_date)

    if eligibility_action in ("TERMINATED", "REINSTATED"):
        change_type = getattr(MemberDailyStatus.ChangeType, eligibility_action)
    elif eligibility_action == "CHANGED":
        change_type = MemberDailyStatus.ChangeType.CHANGED
    elif (
        eligibility_action == "ADDED"
        and change_type == MemberDailyStatus.ChangeType.UNCHANGED
    ):
        change_type = MemberDailyStatus.ChangeType.ADDED

    new_status = derive_coverage_status(member, status_date)
    if new_status != member.coverage_status:
        member.coverage_status = new_status
        member.save(update_fields=["coverage_status"])

    # Keyed on the file as well as the date. The old key was (member,
    # status_date), so when two files carried the same business date — a
    # correction re-sent the same morning, a second sponsor's extract — the
    # second file's row overwrote the first and the fact that the member had
    # appeared in both was gone. Nothing else recorded it, so it was
    # unrecoverable.
    MemberDailyStatus.objects.update_or_create(
        member=member,
        status_date=status_date,
        uploaded_file=source_file,
        defaults={
            "change_type": change_type,
            "changed_fields": changed_fields,
        },
    )

    return member, change_type, changed_fields


def relink_pending_dependents(owner, client=None) -> int:
    """
    Attach dependents that arrived before their subscriber did.

    Called at the end of a file sync, so a dependent whose subscriber turns up
    in a later file — or later in the same file, out of the usual order — gets
    joined up without anyone re-running the import. Matching is on the
    subscriber number the dependent carried on its own REF*0F, which is the
    only link an 834 gives you when the loops arrive out of order.
    """
    pending = Member.objects.filter(
        owner=owner, member_type="DEP", subscriber__isnull=True, subscriber_pending=True
    )
    if client is not None:
        pending = pending.filter(client=client)

    linked = 0
    for dependent in pending.exclude(subscriber_number=""):
        candidates = Member.objects.filter(
            owner=owner,
            member_type="SUB",
        ).exclude(pk=dependent.pk)
        if client is not None:
            candidates = candidates.filter(client=client)

        subscriber = candidates.filter(
            subscriber_number=dependent.subscriber_number
        ).first() or candidates.filter(member_id=dependent.subscriber_number).first()

        if subscriber is None:
            continue

        dependent.subscriber = subscriber
        dependent.subscriber_pending = False
        dependent.save(update_fields=["subscriber", "subscriber_pending"])
        linked += 1

    return linked

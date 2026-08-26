"""
Decide what counts as a change worth showing somebody.

The requirement is narrow and specific: the same SSN appears on the day 1 file
and again on the day 10 file, something about it is different, and that
difference has to reach both the client and the operator rather than being
absorbed silently into the member row. This module is the part that turns a
field-level diff into rows of MemberChangeEvent with enough context to be acted
on.

Three judgements live here rather than in the model, so they can be changed
without a migration.

Which fields are watched at all. Every field the sync writes is compared, but
not every difference is a change anyone wants to read. A postal code gaining
its plus-four, a phone number arriving with punctuation stripped, a middle name
appearing for the first time — those are the file getting more complete, not
the member changing, and a queue full of them is a queue nobody opens.

What category each field belongs to, which is what the filter on the screen
sorts by.

And how much attention each one deserves. This is the judgement that matters
most and the one most likely to need tuning per client. A date of birth
changing is CRITICAL because eligibility is checked against it at the point of
service and a wrong one produces a denied claim; a plan code changing is
CRITICAL because it changes what the member is entitled to and what the plan
pays. A city changing is INFO. A surname changing is REVIEW, because it is
usually a marriage and occasionally two different people collapsed onto one
record by a bad match.

The other rule enforced here: an empty old value is not a change. A field that
was blank and is now populated is the sponsor filling a gap, and reporting
those as changes on the first few files after go-live buries the real ones.
"""

from __future__ import annotations

import logging

from members.models import ChangeCategory, ChangeSeverity

logger = logging.getLogger("edi.change_monitor")


# field -> (category, severity). A field absent from this map is not watched.
WATCHED_FIELDS = {
    # Identity
    "last_name": (ChangeCategory.IDENTITY, ChangeSeverity.REVIEW),
    "first_name": (ChangeCategory.IDENTITY, ChangeSeverity.REVIEW),
    "member_id": (ChangeCategory.IDENTITY, ChangeSeverity.CRITICAL),
    "subscriber_number": (ChangeCategory.IDENTITY, ChangeSeverity.CRITICAL),
    # Demographics. Date of birth and gender are the two that fail an
    # eligibility check outright when they are wrong.
    "date_of_birth": (ChangeCategory.DEMOGRAPHIC, ChangeSeverity.CRITICAL),
    "gender_code": (ChangeCategory.DEMOGRAPHIC, ChangeSeverity.REVIEW),
    "date_of_death": (ChangeCategory.DEMOGRAPHIC, ChangeSeverity.CRITICAL),
    "relationship_code": (ChangeCategory.DEMOGRAPHIC, ChangeSeverity.REVIEW),
    # Address and contact. Real, but rarely urgent.
    "address1": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    "address2": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    "city": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    "state": (ChangeCategory.ADDRESS, ChangeSeverity.REVIEW),
    "postal_code": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    "phone": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    "email": (ChangeCategory.ADDRESS, ChangeSeverity.INFO),
    # Plan and class. What the member is entitled to.
    "plan_code": (ChangeCategory.PLAN, ChangeSeverity.CRITICAL),
    "class_code": (ChangeCategory.PLAN, ChangeSeverity.REVIEW),
    "group_number": (ChangeCategory.PLAN, ChangeSeverity.CRITICAL),
    "local": (ChangeCategory.PLAN, ChangeSeverity.REVIEW),
    "benefit_status_code": (ChangeCategory.PLAN, ChangeSeverity.REVIEW),
    "employment_status_code": (ChangeCategory.PLAN, ChangeSeverity.INFO),
    "student_status_code": (ChangeCategory.PLAN, ChangeSeverity.INFO),
    # Coverage. Written by the eligibility reconciler rather than the field
    # diff, using these synthetic names so they render in the same table.
    "coverage_effective_date": (ChangeCategory.COVERAGE, ChangeSeverity.CRITICAL),
    "coverage_termination_date": (ChangeCategory.COVERAGE, ChangeSeverity.CRITICAL),
    "coverage_status": (ChangeCategory.COVERAGE, ChangeSeverity.CRITICAL),
}

# Human labels for the screen. Keeping them server-side means the change list,
# any export and any future email digest all say the same words.
FIELD_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "middle_name": "Middle name",
    "member_id": "Member ID",
    "subscriber_number": "Subscriber number",
    "date_of_birth": "Date of birth",
    "date_of_death": "Date of death",
    "gender_code": "Gender",
    "relationship_code": "Relationship",
    "address1": "Address line 1",
    "address2": "Address line 2",
    "city": "City",
    "state": "State",
    "postal_code": "ZIP",
    "phone": "Phone",
    "email": "Email",
    "plan_code": "Plan",
    "class_code": "Class",
    "group_number": "Group number",
    "local": "Local",
    "benefit_status_code": "Benefit status",
    "employment_status_code": "Employment status",
    "student_status_code": "Student status",
    "coverage_effective_date": "Coverage effective date",
    "coverage_termination_date": "Coverage termination date",
    "coverage_status": "Coverage status",
}


def label_for(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name.replace("_", " ").capitalize())


def _stringify(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:255]


def is_reportable(field_name: str, old_value, new_value) -> bool:
    """
    Whether this particular difference belongs in the queue.

    Blank-to-populated is excluded on purpose; see the module docstring. So is
    a difference that survives only as whitespace or case, which is a sponsor
    reformatting its extract rather than telling us anything.
    """
    if field_name not in WATCHED_FIELDS:
        return False
    old_text = _stringify(old_value).strip()
    new_text = _stringify(new_value).strip()
    if not old_text:
        return False
    if not new_text:
        # Populated to blank never happens through apply_demographics, which
        # refuses to blank a known value. If it ever does, it is a bug worth
        # seeing rather than a change worth reporting.
        return False
    return old_text.casefold() != new_text.casefold()


def build_events(
    member,
    changed_fields: dict,
    current_file,
    previous_file=None,
    owner=None,
    client=None,
):
    """
    Turn one member's diff into unsaved MemberChangeEvent instances.

    Returned rather than written so the caller can bulk_create a whole batch,
    which is the difference between two queries per file and two per member.
    """
    from members.models import MemberChangeEvent

    events = []
    for field_name, pair in (changed_fields or {}).items():
        try:
            old_value, new_value = pair
        except (TypeError, ValueError):
            logger.debug("Malformed diff entry for %s: %r", field_name, pair)
            continue

        if not is_reportable(field_name, old_value, new_value):
            continue

        category, severity = WATCHED_FIELDS[field_name]
        events.append(
            MemberChangeEvent(
                owner=owner or member.owner,
                client=client if client is not None else member.client,
                member=member,
                ssn_fingerprint=member.ssn_fingerprint or "",
                ssn_last4=member.ssn_last4 or "",
                sponsor_member_id=member.member_id or "",
                member_name=member.full_name[:120],
                member_type=member.member_type,
                field_name=field_name,
                old_value=_stringify(old_value),
                new_value=_stringify(new_value),
                category=category,
                severity=severity,
                previous_file=previous_file,
                current_file=current_file,
                previous_file_date=getattr(previous_file, "file_date", None),
                current_file_date=getattr(current_file, "file_date", None),
            )
        )
    return events


def coverage_event(
    member,
    field_name: str,
    old_value,
    new_value,
    current_file,
    previous_file=None,
    owner=None,
    client=None,
    severity=None,
):
    """
    One coverage change, built from the eligibility reconciler rather than a
    field diff.

    Termination and reinstatement are the two the client asks about first and
    neither is a column on Member, so they cannot come out of the demographic
    comparison. They are recorded here with the same shape so one table and one
    screen cover everything.
    """
    from members.models import MemberChangeEvent

    category, default_severity = WATCHED_FIELDS.get(
        field_name, (ChangeCategory.COVERAGE, ChangeSeverity.REVIEW)
    )
    return MemberChangeEvent(
        owner=owner or member.owner,
        client=client if client is not None else member.client,
        member=member,
        ssn_fingerprint=member.ssn_fingerprint or "",
        ssn_last4=member.ssn_last4 or "",
        sponsor_member_id=member.member_id or "",
        member_name=member.full_name[:120],
        member_type=member.member_type,
        field_name=field_name,
        old_value=_stringify(old_value),
        new_value=_stringify(new_value),
        category=category,
        severity=severity or default_severity,
        previous_file=previous_file,
        current_file=current_file,
        previous_file_date=getattr(previous_file, "file_date", None),
        current_file_date=getattr(current_file, "file_date", None),
    )

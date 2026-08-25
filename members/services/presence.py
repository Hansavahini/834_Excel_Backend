"""
Presence of a member on a given file date.

Two facts are being asked for and they are not the same thing, so both are
computed here rather than being conflated into one boolean.

`in_file` answers "did this person's INS loop appear in the 834 that was
uploaded for this date". That comes from MemberDailyStatus, which the upload
pipeline writes one row of per member per file.

`presence` answers "was this person covered on this date". That comes from the
eligibility spans on the members table — effective_date and termination_date —
because a change-only 834 lists only the people who changed. Judging coverage
by file appearance alone would mark an entire stable roster absent on any day a
sponsor sent a delta file, which is wrong and is the reason the span is the
authority here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from django.db.models import Q

from members.models import Member, MemberDailyStatus, MemberEligibilityHistory

PRESENT = "PRESENT"
ABSENT = "ABSENT"


@dataclass
class SpanView:
    effective_date: Optional[date]
    termination_date: Optional[date]
    plan_code: str
    insurance_line_code: str

    @property
    def is_open(self) -> bool:
        return self.termination_date is None


def covering_span(spans: Iterable[MemberEligibilityHistory], on_date: date):
    """
    The span that covers on_date, preferring an open span, then the latest one
    that started on or before the date. Returns (span_or_None, latest_or_None).
    """
    covering = None
    latest = None
    for span in spans:
        if latest is None or (span.effective_date or date.min) > (
            latest.effective_date or date.min
        ):
            latest = span
        if span.effective_date and span.effective_date > on_date:
            continue
        if span.termination_date is not None and span.termination_date < on_date:
            continue
        if covering is None or span.termination_date is None:
            covering = span
    return covering, latest


def presence_for(member: Member, on_date: date, in_file: bool):
    """
    Resolve one member's presence on one date.

    Returns a dict rather than a tuple because the caller serialises it straight
    to JSON and a positional tuple here has already caused one off-by-one in
    this codebase.
    """
    spans = list(member.eligibility_history.all())
    covering, latest = covering_span(spans, on_date)

    if covering is not None:
        status = PRESENT
        if in_file:
            reason = "Covered on this date and listed in the file."
        else:
            reason = "Covered on this date; not listed in this file."
        span = covering
    elif latest is not None:
        status = ABSENT
        if latest.termination_date and latest.termination_date < on_date:
            reason = "Coverage terminated {d}.".format(d=latest.termination_date)
        elif latest.effective_date and latest.effective_date > on_date:
            reason = "Coverage does not begin until {d}.".format(d=latest.effective_date)
        else:
            reason = "No coverage span covers this date."
        span = latest
    else:
        status = PRESENT if in_file else ABSENT
        reason = (
            "Listed in the file but no eligibility span recorded."
            if in_file
            else "No eligibility span recorded for this member."
        )
        span = None

    return {
        "presence": status,
        "presence_reason": reason,
        "effective_date": span.effective_date if span else None,
        "termination_date": span.termination_date if span else None,
        "span_plan_code": (span.plan_code if span else "") or "",
        "insurance_line_code": (span.insurance_line_code if span else "") or "",
    }


def members_in_file(uploaded_file_id: int):
    """member_id -> MemberDailyStatus for one uploaded file, in one query."""
    rows = MemberDailyStatus.objects.filter(uploaded_file_id=uploaded_file_id)
    return {row.member_id: row for row in rows}


def members_on_date(status_date: date):
    """member_id -> MemberDailyStatus for one business date, in one query."""
    rows = MemberDailyStatus.objects.filter(status_date=status_date)
    return {row.member_id: row for row in rows}


def roster_queryset(owner=None):
    """
    Everyone who could be judged present or absent on some date.

    A member with no eligibility history and no file appearance has never been
    seen and is excluded, otherwise a half-written test fixture shows up as a
    permanently absent person.
    """
    queryset = Member.objects.all()
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    return (
        queryset.filter(
            Q(eligibility_history__isnull=False) | Q(daily_statuses__isnull=False)
        )
        .distinct()
        .select_related("subscriber")
        .prefetch_related("eligibility_history")
    )

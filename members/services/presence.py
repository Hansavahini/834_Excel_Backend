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


def spans_for(member_queryset):
    """
    member_id -> [MemberEligibilityHistory] for every member in the queryset,
    in exactly one SQL query with zero bound parameters.

    This exists because prefetch_related on the roster queryset was the crash.
    Django's prefetch machinery collects the primary key of every member the
    outer query returned and issues WHERE member_id IN (?, ?, ... x N) with one
    bound variable per key. SQLite caps bound variables (999 on the builds most
    distributions ship), so the roster worked in development and failed the day
    a real 834 put a few thousand members in the table. Passing the queryset
    itself as member__in makes the database run it as a subquery — the member
    ids never leave the database, so there is no variable list to overflow, and
    the same SQL is what Postgres would want anyway.
    """
    rows = MemberEligibilityHistory.objects.filter(
        member__in=member_queryset.values("pk")
    ).order_by("member_id", "insurance_line_code", "-effective_date")
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row.member_id, []).append(row)
    return grouped


def presence_for(member: Member, on_date: date, in_file: bool, spans=None):
    """
    Resolve one member's presence on one date.

    Returns a dict rather than a tuple because the caller serialises it straight
    to JSON and a positional tuple here has already caused one off-by-one in
    this codebase.

    spans, when given, is the member's eligibility rows already in hand — see
    spans_for above. Falling back to the related manager is kept for callers
    that resolve one member at a time, where a single extra query is fine.
    """
    if spans is None:
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
    # No prefetch_related here, on purpose. Prefetching eligibility_history
    # across the whole roster is what raised "too many SQL variables" on
    # SQLite once the member count passed the bound-parameter cap. The roster
    # view loads the spans itself through spans_for(), which is one subquery
    # instead of one IN list per thousand members.
    return (
        queryset.filter(
            Q(eligibility_history__isnull=False) | Q(daily_statuses__isnull=False)
        )
        .distinct()
        .select_related("subscriber")
    )

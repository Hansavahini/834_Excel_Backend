"""
Write one parsed 834 member loop into the members tables.

What this version changes, and why it was worth changing.

The engine was correct and slow, in a specific and measurable way. Every INS
loop cost between twenty-five and thirty-one database round trips: two to five
to work out who the person was, one to load their coverage spans, another to
load the same spans again inside the master-table projection, a third to load
them once more to derive coverage status, a full-column UPDATE whether or not
anything had moved, then four more separate single-column UPDATEs, then two
update_or_create pairs. On SQLite each round trip is roughly half a millisecond,
so a six thousand loop file spent about seventy-six seconds doing database work
that amounts to a few seconds of actual writing.

The worst of it was that this held even when nothing had changed. A sponsor that
sends its whole roster every morning - which is the normal arrangement, not an
unusual one - paid the full seventy-six seconds every day to discover that all
six thousand people were exactly as they were yesterday.

Three changes, in order of how much they are worth.

Identity comes from memory. RosterIndex loads the client's roster in three
queries at the start of a run and answers "who is this" from a dictionary. That
removes five to eight queries per loop on its own.

A loop whose content digest matches what is stored is skipped entirely. Not
"updated to the same values" - skipped: no SELECT, no UPDATE, no projection, no
span reconciliation. The only thing an unchanged person costs is a presence row
and an enrollment row, and both of those are buffered and written in bulk. This
is also the plain reading of "if a member already exists, do not store them
again", and having it be the fast path rather than a check bolted on the front
is what makes it trustworthy: there is no path where a duplicate could be
written, because the code that would write one does not run.

Writes are batched. MemberDailyStatus, EnrollmentRecord and MemberChangeEvent
are accumulated in a SyncContext and bulk_created a few thousand at a time, with
ignore_conflicts standing in for the update_or_create round trip. The unique
constraints on those tables are what make that safe: a re-run inserts nothing
rather than duplicating, and the constraint is enforced by the database rather
than by this module remembering to check.

Everything the previous version got right is unchanged. Every parsed field is
still written. Spans are still reconciled rather than replaced. A dependent
whose subscriber has not arrived is still flagged rather than promoted to
subscriber. coverage_status is still derived in one place from the spans.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone

from members.models import (
    CoverageStatus,
    Member,
    MemberChangeEvent,
    MemberDailyStatus,
    MemberEligibilityHistory,
)
from members.models import ssn_fingerprint as compute_ssn_fingerprint

from .change_monitor import build_events, coverage_event
from .digest import loop_digest
from .identity import resolve_member_identity
from .roster_sync import project_member

logger = logging.getLogger("edi.member_sync")

TERMINATION_CODE = "024"
REINSTATEMENT_CODE = "025"

# How many buffered rows accumulate before they are written. Large enough that
# the round trip is amortised to nothing, small enough that a batch is still a
# sensible rollback unit and the memory it holds is bounded.
FLUSH_EVERY = 2000

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
#
# Note this is now a superset of what actually reaches the change queue:
# change_monitor.WATCHED_FIELDS decides that, with a category and a severity
# per field. This list decides only what goes in the compact changed_fields
# blob on the daily status row, which the member card renders.
NOTABLE_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
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
    "group_number",
    "local",
    "subscriber_number",
    "relationship_code",
    "benefit_status_code",
    "employment_status_code",
    "student_status_code",
)


def _clean(value, default=""):
    """The models use blank strings, not nulls, for every optional CharField."""
    return default if value is None else value


def _same(left, right) -> bool:
    """Whether two values would render identically once stored."""

    def text(value):
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value).strip()

    return text(left) == text(right)


# ---------------------------------------------------------------------------
# SyncContext - the per-file state that makes batching possible
# ---------------------------------------------------------------------------


class SyncContext:
    """
    Everything one file's sync needs to know, plus the buffers it writes through.

    This exists so the per-loop function has somewhere to put a row without
    issuing a query for it. Callers that do not want batching (a test, a single
    loop, the admin) can leave it out entirely: sync_member_loop builds a
    throwaway context that flushes on every call, which is exactly the old
    behaviour at the old cost.
    """

    __slots__ = (
        "owner",
        "client",
        "source_file",
        "status_date",
        "index",
        "autoflush",
        "daily",
        "enrollments",
        "events",
        "counters",
        "_files",
    )

    def __init__(self, owner, client, source_file, status_date, index=None, autoflush=False):
        self.owner = owner
        self.client = client
        self.source_file = source_file
        self.status_date = status_date
        self.index = index
        self.autoflush = autoflush
        self.daily = []
        self.enrollments = []
        self.events = []
        self.counters = {"skipped": 0, "written": 0, "events": 0}
        # UploadedFile rows this run has needed, keyed by pk. Tiny by nature -
        # a member has been seen in at most as many files as the client has
        # uploaded - and it removes one lazy FK fetch per changed loop.
        self._files = {source_file.pk: source_file} if getattr(source_file, "pk", None) else {}

    def file_by_id(self, file_id):
        """The UploadedFile with this pk, fetched at most once per run."""
        if not file_id:
            return None
        if file_id not in self._files:
            from files.models import UploadedFile

            self._files[file_id] = UploadedFile.objects.filter(pk=file_id).first()
        return self._files[file_id]

    # -- buffering ----------------------------------------------------

    def add_daily(self, row):
        self.daily.append(row)
        self._maybe_flush()

    def add_enrollments(self, rows):
        if rows:
            self.enrollments.extend(rows)
            self._maybe_flush()

    def add_events(self, rows):
        if rows:
            self.events.extend(rows)
            self.counters["events"] += len(rows)
            self._maybe_flush()

    def _maybe_flush(self):
        if self.autoflush:
            self.flush()
            return
        if (
            len(self.daily) >= FLUSH_EVERY
            or len(self.enrollments) >= FLUSH_EVERY
            or len(self.events) >= FLUSH_EVERY
        ):
            self.flush()

    def flush(self):
        """
        Write everything buffered, in three statements rather than three per row.

        ignore_conflicts is doing real work here and is safe for a specific
        reason: each of these tables carries a unique constraint that says what
        a duplicate is, so a row the database already holds is a row this run
        does not need to write. That is what makes a re-run of the same file a
        no-op instead of an integrity error, and it is why the constraints were
        worth having in the first place.
        """
        from members.models import EnrollmentRecord

        if self.daily:
            MemberDailyStatus.objects.bulk_create(
                self.daily, batch_size=500, ignore_conflicts=True
            )
            self.daily = []
        if self.enrollments:
            EnrollmentRecord.objects.bulk_create(
                self.enrollments, batch_size=500, ignore_conflicts=True
            )
            self.enrollments = []
        if self.events:
            MemberChangeEvent.objects.bulk_create(
                self.events, batch_size=500, ignore_conflicts=True
            )
            self.events = []


# ---------------------------------------------------------------------------
# Field comparison
# ---------------------------------------------------------------------------


def get_changed_fields(member, parsed_dict: dict) -> dict:
    """Field level diff between what is stored and what the file says."""
    changed = {}
    for field in NOTABLE_FIELDS:
        new_val = parsed_dict.get(field)
        if new_val in (None, ""):
            continue
        old_val = getattr(member, field, None)
        if old_val == new_val:
            continue
        changed[field] = [
            old_val.isoformat() if hasattr(old_val, "isoformat") else old_val,
            new_val.isoformat() if hasattr(new_val, "isoformat") else new_val,
        ]
    return changed


def apply_demographics(member, parsed_dict: dict, save: bool = True) -> list:
    """
    Copy every parsed field onto the member, never blanking a known value.

    Returns the list of attributes it actually touched. That return is the point
    of the rewrite: the previous version ended in a bare member.save(), which
    rewrites all thirty-odd columns and the two derived SSN columns on every
    loop of every file whether or not one byte differed. Handing the caller the
    changed set lets it save with update_fields, and lets it skip the save
    entirely when the set is empty.
    """
    touched = []
    for field in DEMOGRAPHIC_FIELDS:
        new_val = parsed_dict.get(field)
        if new_val in (None, ""):
            continue
        if getattr(member, field) != new_val:
            setattr(member, field, new_val)
            touched.append(field)

    if parsed_dict.get("member_id") and not member.member_id:
        member.member_id = parsed_dict["member_id"]
        touched.append("member_id")
    if parsed_dict.get("ssn") and not member.ssn:
        member.ssn = parsed_dict["ssn"]
        touched.extend(["ssn", "ssn_fingerprint", "ssn_last4"])
    relationship = parsed_dict.get("relationship_code")
    if relationship and member.relationship_code != relationship:
        member.relationship_code = relationship
        touched.append("relationship_code")

    if save and touched:
        member.save(update_fields=tuple(dict.fromkeys(touched)))
    return touched


# ---------------------------------------------------------------------------
# Eligibility spans
# ---------------------------------------------------------------------------


def _spans_for(member, cache: Optional[dict] = None):
    """
    This member's coverage spans, loaded at most once per loop.

    Three separate places used to ask for them - the span reconciler, the
    coverage-status derivation and the master-table projection - and each got
    its own query because each built a fresh queryset. They now share one list,
    which the reconciler keeps current in memory as it writes.
    """
    if cache is not None and "spans" in cache:
        return cache["spans"]
    spans = list(member.eligibility_history.all())
    if cache is not None:
        cache["spans"] = spans
    return spans


def derive_coverage_status(member, as_of=None, spans=None) -> str:
    """
    One place that decides ACTIVE versus TERMINATED, read from the spans.

    Anything with an open span, or a span that has not yet closed as of the
    date being processed, is active. Everything else with at least one span is
    terminated. No spans at all stays UNKNOWN rather than being guessed.
    """
    as_of = as_of or timezone.now().date()
    if spans is None:
        spans = list(member.eligibility_history.all())
    if not spans:
        return CoverageStatus.UNKNOWN
    for span in spans:
        if span.termination_date is None or span.termination_date >= as_of:
            return CoverageStatus.ACTIVE
    return CoverageStatus.TERMINATED


def sync_eligibility(member, parsed_dict: dict, source_file, status_date, cache=None):
    """
    Reconcile every coverage line in the loop against the stored spans.

    Returns (strongest action, [(field, old, new), ...]) where the second
    element is the coverage movement the change monitor records. Termination and
    reinstatement are not columns on Member, so they cannot come out of the
    demographic diff and have to be reported from here.

    Ordering of the action is TERMINATED > REINSTATED > CHANGED > ADDED >
    UNCHANGED, which is what the daily status row records.
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
    movements = []

    spans = _spans_for(member, cache)

    def record(action):
        nonlocal outcome
        if ranking[action] > ranking[outcome]:
            outcome = action

    def open_span_for(line):
        candidates = [
            span
            for span in spans
            if span.insurance_line_code == line and span.termination_date is None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda span: span.effective_date)

    def span_starting(line, start):
        for span in spans:
            if span.insurance_line_code == line and span.effective_date == start:
                return span
        return None

    for coverage in coverages:
        line = (coverage.get("insurance_line_code") or "HLT")[:3]
        plan = _clean(coverage.get("plan_code"))[:30]
        mtc = _clean(coverage.get("maintenance_type_code"))[:3]
        effective = coverage.get("effective_date")
        termination = coverage.get("termination_date")

        # A termination is either an explicit 024 or an end date on the line.
        is_terminating = mtc == TERMINATION_CODE or termination is not None

        open_span = open_span_for(line)

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
                open_span.save(update_fields=["termination_date", "maintenance_type_code"])
                movements.append(("coverage_termination_date", "", end_date))
                record("TERMINATED")
            elif effective:
                # Terminating a span nobody recorded. Write the closed span so
                # the history is complete rather than dropping the fact.
                span = span_starting(line, effective)
                if span is None:
                    span, _created = MemberEligibilityHistory.objects.get_or_create(
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
                    spans.append(span)
                elif span.termination_date is None:
                    span.termination_date = max(end_date, span.effective_date)
                    span.save(update_fields=["termination_date"])
                movements.append(("coverage_termination_date", "", end_date))
                record("TERMINATED")
            continue

        start = effective or status_date

        if open_span is None:
            span = span_starting(line, start)
            if span is None:
                span = MemberEligibilityHistory.objects.create(
                    member=member,
                    insurance_line_code=line,
                    effective_date=start,
                    plan_code=plan,
                    class_code=_clean(parsed_dict.get("class_code"))[:30],
                    maintenance_type_code=mtc,
                    source_file=source_file,
                )
                spans.append(span)
                movements.append(("coverage_effective_date", "", start))
                record("REINSTATED" if mtc == REINSTATEMENT_CODE else "ADDED")
            elif span.termination_date is not None:
                # A file reopening a span that was previously closed.
                previous_end = span.termination_date
                span.termination_date = None
                span.maintenance_type_code = mtc or span.maintenance_type_code
                span.save(update_fields=["termination_date", "maintenance_type_code"])
                movements.append(("coverage_status", "TERMINATED", "ACTIVE"))
                movements.append(("coverage_termination_date", previous_end, ""))
                record("REINSTATED")
            continue

        # An open span already exists for this line.
        if plan and open_span.plan_code != plan:
            previous_plan = open_span.plan_code
            close_on = start if start > open_span.effective_date else open_span.effective_date
            open_span.termination_date = close_on
            open_span.save(update_fields=["termination_date"])

            span = span_starting(line, start)
            if span is None:
                span = MemberEligibilityHistory.objects.create(
                    member=member,
                    insurance_line_code=line,
                    effective_date=start,
                    plan_code=plan,
                    class_code=_clean(parsed_dict.get("class_code"))[:30],
                    maintenance_type_code=mtc,
                    source_file=source_file,
                )
                spans.append(span)
            else:
                span.plan_code = plan
                span.termination_date = None
                span.save(update_fields=["plan_code", "termination_date"])
            movements.append(("coverage_effective_date", open_span.effective_date, start))
            if previous_plan:
                movements.append(("plan_code", previous_plan, plan))
            record("CHANGED")

    return outcome, movements


# ---------------------------------------------------------------------------
# The per-loop entry point
# ---------------------------------------------------------------------------


class _LightMember:
    """
    A stand-in returned by the fast path.

    Callers only ever read pk and member_type from what sync_member_loop hands
    back on an unchanged loop, and fetching a full Member to satisfy that would
    reintroduce the query the fast path exists to avoid. Anything that genuinely
    needs the row selects it by pk.
    """

    __slots__ = ("pk", "id", "member_type", "subscriber_id", "is_light")

    def __init__(self, pk, parsed_dict):
        self.pk = pk
        self.id = pk
        self.member_type = parsed_dict.get("member_type") or "SUB"
        self.subscriber_id = None
        self.is_light = True


def sync_member_loop(
    parsed_dict: dict,
    owner,
    source_file,
    status_date,
    current_subscriber=None,
    client=None,
    context: Optional[SyncContext] = None,
):
    """
    Main entry point, called once per INS loop.

    Returns (member, change_type, changed_fields).

    context is optional. Without one, a throwaway autoflushing context is built
    so a single call behaves exactly as it always did; the file-level sync
    passes a real one so the presence, enrollment and change rows are buffered
    and written in bulk.
    """
    if client is None:
        client = getattr(source_file, "client", None)

    if context is None:
        context = SyncContext(owner, client, source_file, status_date, autoflush=True)

    index = context.index

    # The fingerprint is computed once here and carried on the dict, so neither
    # the index lookup nor the query fallback has to recompute an HMAC.
    if "ssn_fingerprint" not in parsed_dict:
        parsed_dict["ssn_fingerprint"] = (
            compute_ssn_fingerprint(parsed_dict.get("ssn")) if parsed_dict.get("ssn") else ""
        )

    digest = loop_digest(parsed_dict)

    # -------------------------------------------------------------------
    # The fast path.
    #
    # A person already on file whose loop digests to exactly what last wrote
    # them has, by construction, nothing for the write path to do. Their
    # presence in this file is still recorded - that is the history the change
    # monitor and the member card read - but the member row, the spans and the
    # master tables are not touched at all.
    # -------------------------------------------------------------------
    if index is not None and index.usable:
        member_pk = index.find_member(
            parsed_dict, current_subscriber_pk=getattr(current_subscriber, "pk", None)
        )
        if member_pk is not None and index.digest_of(member_pk) == digest:
            if _record_unchanged(context, member_pk, parsed_dict, index):
                context.counters["skipped"] += 1
                return (
                    _LightMember(member_pk, parsed_dict),
                    MemberDailyStatus.ChangeType.UNCHANGED,
                    {},
                )

    return _sync_slow(
        parsed_dict,
        owner,
        source_file,
        status_date,
        current_subscriber,
        client,
        context,
        digest,
    )


def _record_unchanged(context, member_pk, parsed_dict, index) -> bool:
    """
    Presence and enrollment for somebody nothing has changed about.

    Returns False when the person has no master row yet - a member created
    before the projection existed - so the caller falls through to the slow
    path and builds one rather than quietly losing their enrollment history.
    """
    member_type = parsed_dict.get("member_type") or "SUB"
    roster_pk = index.roster_pk(member_pk, member_type)
    if roster_pk is None:
        return False

    from members.models import EnrollmentRecord

    context.add_daily(
        MemberDailyStatus(
            member_id=member_pk,
            uploaded_file=context.source_file,
            status_date=context.status_date,
            change_type=MemberDailyStatus.ChangeType.UNCHANGED,
            changed_fields={},
        )
    )

    rows = []
    for coverage in parsed_dict.get("coverages") or []:
        effective = coverage.get("effective_date")
        termination = coverage.get("termination_date")
        if termination and effective and termination < effective:
            termination = effective
        rows.append(
            EnrollmentRecord(
                owner=context.owner,
                client=context.client,
                subscriber_id=roster_pk if member_type == "SUB" else None,
                dependant_id=None if member_type == "SUB" else roster_pk,
                source_file=context.source_file,
                file_date=getattr(context.source_file, "file_date", None),
                plan=(coverage.get("plan_code") or "")[:30],
                insurance_line_code=(coverage.get("insurance_line_code") or "HLT")[:3],
                effective_date=effective,
                termination_date=termination,
                maintenance_type_code=(coverage.get("maintenance_type_code") or "")[:3],
                relationship=parsed_dict.get("relationship_code") or "",
                member_type=member_type,
            )
        )
    context.add_enrollments(rows)
    return True


@transaction.atomic
def _sync_slow(
    parsed_dict, owner, source_file, status_date, current_subscriber, client, context, digest
):
    """The full write path, for a person who is new or who has actually moved."""
    index = context.index
    cache = {}

    member = None
    if index is not None and index.usable:
        member_pk = index.find_member(
            parsed_dict, current_subscriber_pk=getattr(current_subscriber, "pk", None)
        )
        if member_pk is not None:
            member = Member.objects.filter(pk=member_pk).first()
    else:
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
        logger.debug(
            "Dependent %s %s arrived with no subscriber in scope; flagged pending linkage.",
            parsed_dict.get("first_name"),
            parsed_dict.get("last_name"),
        )

    change_type = MemberDailyStatus.ChangeType.UNCHANGED
    changed_fields = {}
    previous_file = None
    is_new = member is None

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
            content_digest=digest,
        )
        for field in DEMOGRAPHIC_FIELDS:
            value = parsed_dict.get(field)
            if value not in (None, ""):
                setattr(member, field, value)
        member.save()
        cache["spans"] = []
        change_type = MemberDailyStatus.ChangeType.ADDED
    else:
        # member.last_seen_file would lazily SELECT the UploadedFile row, once
        # per loop, for a table that holds a handful of rows. The context keeps
        # the ones this run has touched, so the change monitor can name the
        # previous file without a query.
        previous_file = context.file_by_id(member.last_seen_file_id)
        changed_fields = get_changed_fields(member, parsed_dict)

        # One save, not five. Collect everything that moved and write it once
        # with update_fields; the previous version issued a full-column UPDATE
        # followed by up to four single-column ones.
        touched = apply_demographics(member, parsed_dict, save=False)

        if (
            member.member_type == "DEP"
            and member.subscriber_id is None
            and current_subscriber is not None
            and current_subscriber.pk != member.pk
        ):
            member.subscriber = current_subscriber
            member.subscriber_pending = False
            touched.extend(["subscriber", "subscriber_pending"])
        elif member.member_type == "DEP" and member.subscriber_id is None:
            if not member.subscriber_pending:
                member.subscriber_pending = True
                touched.append("subscriber_pending")

        if client is not None and member.client_id is None:
            member.client = client
            touched.append("client")

        if member.last_seen_file_id != getattr(source_file, "pk", None):
            member.last_seen_file = source_file
            touched.append("last_seen_file")

        if member.content_digest != digest:
            member.content_digest = digest
            touched.append("content_digest")

        if touched:
            member.save(update_fields=tuple(dict.fromkeys(touched)))

        if changed_fields:
            change_type = MemberDailyStatus.ChangeType.CHANGED

    eligibility_action, movements = sync_eligibility(
        member, parsed_dict, source_file, status_date, cache=cache
    )

    if eligibility_action in ("TERMINATED", "REINSTATED"):
        change_type = getattr(MemberDailyStatus.ChangeType, eligibility_action)
    elif eligibility_action == "CHANGED":
        change_type = MemberDailyStatus.ChangeType.CHANGED
    elif (
        eligibility_action == "ADDED"
        and change_type == MemberDailyStatus.ChangeType.UNCHANGED
    ):
        change_type = MemberDailyStatus.ChangeType.ADDED

    new_status = derive_coverage_status(member, status_date, spans=cache.get("spans"))
    if new_status != member.coverage_status:
        member.coverage_status = new_status
        member.save(update_fields=["coverage_status"])

    # Keyed on the file as well as the date. The old key was (member,
    # status_date), so when two files carried the same business date - a
    # correction re-sent the same morning, a second sponsor's extract - the
    # second file's row overwrote the first and the fact that the member had
    # appeared in both was gone.
    context.add_daily(
        MemberDailyStatus(
            member_id=member.pk,
            uploaded_file=source_file,
            status_date=status_date,
            change_type=change_type,
            changed_fields=changed_fields,
        )
    )

    # -------------------------------------------------------------------
    # The change monitor. Only for a member who was already on file: an
    # addition is not a change, and reporting six thousand of them on the
    # first upload would make the queue useless on day one.
    # -------------------------------------------------------------------
    if change_type != MemberDailyStatus.ChangeType.ADDED:
        events = build_events(
            member,
            changed_fields,
            current_file=source_file,
            previous_file=previous_file,
            owner=owner,
            client=client,
        )
        seen_fields = {event.field_name for event in events}
        for field_name, old_value, new_value in movements:
            if field_name in seen_fields:
                continue
            # A movement whose two sides are equal is not a movement. The span
            # reconciler reports the effective date whenever it rewrites a
            # coverage line, and on a plan change that keeps the same start date
            # both sides are identical - which would put a row reading
            # "01-01-2024 to 01-01-2024" in the queue next to the plan change
            # that actually mattered.
            if _same(old_value, new_value):
                continue
            seen_fields.add(field_name)
            events.append(
                coverage_event(
                    member,
                    field_name,
                    old_value,
                    new_value,
                    current_file=source_file,
                    previous_file=previous_file,
                    owner=owner,
                    client=client,
                )
            )
        context.add_events(events)

    # Parts 2 and 3: keep the separated master tables in step with this member.
    # Inside the same atomic block on purpose - a Member row whose Subscriber
    # or Dependant projection failed would be a person the roster can see and
    # the master tables cannot, which is the sort of split-brain that only shows
    # up in a reconciliation months later.
    # The projection needs the member's spans to fill effective/termination on
    # the master row. sync_eligibility has just loaded and updated them, so hand
    # that list over rather than letting project_member issue its own query -
    # which would also risk reading a span this transaction has just written.
    parsed_dict["__spans__"] = cache.get("spans")
    roster_record = project_member(
        member,
        parsed_dict,
        source_file,
        owner,
        client,
        index=index,
        context=context,
        is_new=is_new,
    )

    if index is not None and index.usable:
        index.remember(member, digest=digest)
        index.remember_roster(member.pk, member.member_type, roster_record)

    context.counters["written"] += 1
    return member, change_type, changed_fields


# ---------------------------------------------------------------------------
# Relinking
# ---------------------------------------------------------------------------


def _subscriber_or_member_id(values):
    """A single OR over the two columns a dependant's REF*0F can match."""
    from django.db.models import Q

    return Q(subscriber_number__in=values) | Q(member_id__in=values)


def relink_pending_dependents(owner, client=None) -> int:
    """
    Attach dependents that arrived before their subscriber did.

    Called at the end of a file sync, so a dependent whose subscriber turns up
    in a later file - or later in the same file, out of the usual order - gets
    joined up without anyone re-running the import. Matching is on the
    subscriber number the dependent carried on its own REF*0F, which is the only
    link an 834 gives you when the loops arrive out of order.

    Rewritten to three queries and a dictionary rather than two queries per
    pending dependant. On a change file that lists a family's dependants before
    their subscriber, the old per-row lookups were a meaningful part of the tail
    of the run.
    """
    pending = Member.objects.filter(
        owner=owner, member_type="DEP", subscriber__isnull=True, subscriber_pending=True
    ).exclude(subscriber_number="")
    if client is not None:
        pending = pending.filter(client=client)

    pending_rows = list(pending.values_list("pk", "subscriber_number"))
    if not pending_rows:
        return 0

    wanted = {number for _pk, number in pending_rows}

    candidates = Member.objects.filter(owner=owner, member_type="SUB")
    if client is not None:
        candidates = candidates.filter(client=client)

    by_subscriber_number = {}
    by_member_id = {}
    for pk, subscriber_number, member_id in candidates.filter(
        _subscriber_or_member_id(wanted)
    ).values_list("pk", "subscriber_number", "member_id"):
        if subscriber_number and subscriber_number not in by_subscriber_number:
            by_subscriber_number[subscriber_number] = pk
        if member_id and member_id not in by_member_id:
            by_member_id[member_id] = pk

    linked = 0
    for pk, number in pending_rows:
        subscriber_pk = by_subscriber_number.get(number) or by_member_id.get(number)
        if subscriber_pk is None or subscriber_pk == pk:
            continue
        Member.objects.filter(pk=pk).update(
            subscriber_id=subscriber_pk, subscriber_pending=False
        )
        linked += 1

    return linked

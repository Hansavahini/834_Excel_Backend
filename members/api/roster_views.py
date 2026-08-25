"""
Admin-only roster endpoints behind the Info section.

Three endpoints, deliberately small:

  file-dates/   the dates that have an 834 behind them, newest first
  ssn-options/  the SSN dropdown, masked for display, keyed on the digits
  roster/       the roster itself for one date, filtered and paginated

Everything is IsAdminUser. The Info section is described as admin side only and
a permission check in the browser is a suggestion, not a control, so the gate
lives here as well as in the router.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from files.models import UploadedFile
from members.models import Member, ssn_fingerprint
from users.tenancy import resolve_client, scope_to_client
from members.services.presence import (
    ABSENT,
    PRESENT,
    members_in_file,
    members_on_date,
    presence_for,
    roster_queryset,
)

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _dated_files(owner=None, client=None):
    """Uploaded 834s that carry a business date, newest first."""
    queryset = UploadedFile.objects.filter(file_date__isnull=False)
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    if client is not None:
        queryset = scope_to_client(queryset, client)
    return queryset.order_by("-file_date", "-uploaded_at")


def _owner_scope(request):
    """
    Staff see the whole roster; the scope=mine switch narrows it to their own
    uploads, which is how support reproduces what one operator sees.
    """
    if request.query_params.get("scope") == "mine":
        return request.user
    return None


class FileDatesView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        owner = _owner_scope(request)
        client = resolve_client(request)
        seen = {}
        for item in _dated_files(owner, client).select_related("owner")[:400]:
            if item.file_date in seen:
                continue
            seen[item.file_date] = {
                "file_date": item.file_date,
                "uploaded_file_id": item.id,
                "file_name": item.original_filename,
                "is_full_file": item.is_full_file,
                "member_loop_count": item.member_loop_count,
                "processing_status": item.processing_status,
                "sponsor_name": item.sponsor_name,
            }
        return Response(list(seen.values()))


class SSNOptionsView(APIView):
    """
    Values for the SSN dropdown.

    The value used to be the SSN itself, so populating a filter dropdown sent
    every member's nine digits to the browser in one response. The value is now
    the fingerprint, which is opaque, already stored, and all the roster filter
    needs in order to match — the label carries the mask a human reads.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        owner = _owner_scope(request)
        queryset = Member.objects.exclude(ssn_fingerprint="")
        if owner is not None:
            queryset = queryset.filter(owner=owner)
        client = resolve_client(request)
        if client is not None:
            queryset = scope_to_client(queryset, client)

        options = []
        seen = set()
        for member in queryset.order_by("last_name", "first_name").only(
            "ssn_fingerprint", "ssn_last4", "ssn",
            "first_name", "middle_name", "last_name", "member_type",
        )[:2000]:
            if member.ssn_fingerprint in seen:
                continue
            seen.add(member.ssn_fingerprint)
            options.append(
                {
                    "value": member.ssn_fingerprint,
                    "masked": member.masked_ssn,
                    "label": "{masked} — {name}".format(
                        masked=member.masked_ssn, name=member.full_name
                    ),
                    "name": member.full_name,
                    "member_type": member.member_type,
                }
            )
        return Response(options)


class MemberRosterView(APIView):
    """
    The roster for one file date, with presence resolved per member.

    Query parameters
      file_date         YYYY-MM-DD. Defaults to the newest dated upload.
      uploaded_file_id  pick a specific file instead of a date
      ssn               the opaque fingerprint from the dropdown, a full SSN,
                        or the last four digits
      q                 name, member id, full SSN or last four digits
      presence          PRESENT | ABSENT | ALL
      member_type       SUB | DEP | ALL
      page, page_size
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        owner = _owner_scope(request)
        client = resolve_client(request)
        params = request.query_params

        selected = None
        requested_id = params.get("uploaded_file_id")
        if requested_id:
            selected = _dated_files(owner, client).filter(pk=requested_id).first()
            if selected is None:
                return Response(
                    {"detail": "That uploaded file does not exist or has no file date."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        wanted_date = _parse_date(params.get("file_date"))
        if selected is None and wanted_date:
            selected = _dated_files(owner, client).filter(file_date=wanted_date).first()
            if selected is None:
                return Response(
                    {
                        "detail": "No 834 has been uploaded for {d}.".format(d=wanted_date),
                        "file_date": wanted_date,
                        "results": [],
                        "counts": {"total": 0, "present": 0, "absent": 0, "in_file": 0},
                        "count": 0,
                        "page": 1,
                        "page_size": DEFAULT_PAGE_SIZE,
                        "total_pages": 0,
                    },
                    status=status.HTTP_200_OK,
                )

        if selected is None:
            selected = _dated_files(owner, client).first()

        if selected is None:
            return Response(
                {
                    "detail": "No 834 files with a business date have been uploaded yet.",
                    "file": None,
                    "file_date": None,
                    "results": [],
                    "counts": {"total": 0, "present": 0, "absent": 0, "in_file": 0},
                    "count": 0,
                    "page": 1,
                    "page_size": DEFAULT_PAGE_SIZE,
                    "total_pages": 0,
                }
            )

        on_date = selected.file_date

        # Presence in the file is keyed on the business date, not on one upload
        # row, so a corrected re-send for the same date still counts.
        appearances = members_on_date(on_date)
        if not appearances:
            appearances = members_in_file(selected.id)

        queryset = roster_queryset(owner)
        if client is not None:
            queryset = scope_to_client(queryset, client)

        # Accepts a full SSN typed by a human, the last four digits, or the
        # opaque fingerprint the dropdown supplies. None of these paths needs
        # the plaintext column, so the filter keeps working after a purge.
        ssn = (params.get("ssn") or "").replace("-", "").strip()
        if ssn:
            digits = "".join(ch for ch in ssn if ch.isdigit())
            if len(digits) == 9:
                queryset = queryset.filter(ssn_fingerprint=ssn_fingerprint(digits))
            elif len(digits) == 4 and len(ssn) == 4:
                queryset = queryset.filter(ssn_last4=digits)
            else:
                queryset = queryset.filter(ssn_fingerprint=ssn)

        member_type = (params.get("member_type") or "ALL").upper()
        if member_type in ("SUB", "DEP"):
            queryset = queryset.filter(member_type=member_type)

        search = (params.get("q") or "").strip()
        if search:
            digits = "".join(ch for ch in search if ch.isdigit())
            criteria = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(middle_name__icontains=search)
                | Q(member_id__icontains=search)
            )
            # "john smith" should match across the two name columns.
            parts = [p for p in search.split() if p]
            if len(parts) > 1:
                criteria |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
                criteria |= Q(first_name__icontains=parts[-1]) & Q(
                    last_name__icontains=parts[0]
                )
            if len(digits) == 9:
                criteria |= Q(ssn_fingerprint=ssn_fingerprint(digits))
            elif len(digits) == 4:
                criteria |= Q(ssn_last4=digits)
            queryset = queryset.filter(criteria)

        rows = []
        totals = {"total": 0, "present": 0, "absent": 0, "in_file": 0}

        for member in queryset.order_by("last_name", "first_name", "id"):
            appearance = appearances.get(member.id)
            in_file = appearance is not None
            resolved = presence_for(member, on_date, in_file)

            totals["total"] += 1
            totals["present" if resolved["presence"] == PRESENT else "absent"] += 1
            if in_file:
                totals["in_file"] += 1

            subscriber = member.subscriber
            rows.append(
                {
                    "id": member.id,
                    "full_name": member.full_name,
                    "first_name": member.first_name,
                    "last_name": member.last_name,
                    "member_type": member.member_type,
                    "member_type_display": (
                        "Subscriber" if member.member_type == "SUB" else "Dependent"
                    ),
                    "relationship_code": member.relationship_code,
                    "relationship_display": member.get_relationship_code_display(),
                    "member_id": member.member_id,
                    "subscriber_name": subscriber.full_name if subscriber else "",
                    "subscriber_number": member.subscriber_number,
                    "group_number": member.group_number,
                    # Masked only. A roster page renders hundreds of rows; the
                    # plaintext was being sent for every one of them.
                    "masked_ssn": member.masked_ssn,
                    "date_of_birth": member.date_of_birth,
                    "gender_code": member.gender_code,
                    "plan_code": member.plan_code or resolved["span_plan_code"],
                    "class_code": member.class_code,
                    "coverage_status": member.coverage_status,
                    "effective_date": resolved["effective_date"],
                    "termination_date": resolved["termination_date"],
                    "insurance_line_code": resolved["insurance_line_code"],
                    "presence": resolved["presence"],
                    "presence_reason": resolved["presence_reason"],
                    "in_file": in_file,
                    "change_type": appearance.change_type if appearance else None,
                    "changed_fields": (
                        list(appearance.changed_fields.keys())
                        if appearance and appearance.changed_fields
                        else []
                    ),
                }
            )

        presence_filter = (params.get("presence") or "ALL").upper()
        if presence_filter in (PRESENT, ABSENT):
            rows = [row for row in rows if row["presence"] == presence_filter]

        if (params.get("in_file") or "").lower() in ("true", "1", "yes"):
            rows = [row for row in rows if row["in_file"]]

        try:
            page = max(1, int(params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(params.get("page_size", DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        count = len(rows)
        total_pages = (count + page_size - 1) // page_size or 0
        if total_pages and page > total_pages:
            page = total_pages
        start = (page - 1) * page_size

        return Response(
            {
                "file": {
                    "id": selected.id,
                    "file_name": selected.original_filename,
                    "file_date": selected.file_date,
                    "is_full_file": selected.is_full_file,
                    "member_loop_count": selected.member_loop_count,
                    "processing_status": selected.processing_status,
                    "sponsor_name": selected.sponsor_name,
                    "uploaded_at": selected.uploaded_at,
                },
                "file_date": selected.file_date,
                "counts": totals,
                "count": count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "results": rows[start : start + page_size],
            }
        )

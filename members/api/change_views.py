"""
The change monitor's read and review endpoints.

Three of them, and the split between them is deliberate.

  changes/          the queue itself, filtered and paginated
  changes/summary/  the counts the header tiles show
  changes/<pk>/     acknowledge one change, with a note

The summary is a separate endpoint rather than a block bolted onto the list
response because the two have different cache lives and very different costs.
The counts are five aggregate queries over an indexed column and are wanted on
every page load; the list is a page of rows and is re-fetched every time a
filter moves. Folding them together would mean recomputing the aggregates on
every filter change for no reason.

Everything is scoped by owner and by client, in that order, through the same
tenancy helpers the rest of the API uses. A change event carries PHI-adjacent
data about a named person, so a change list leaking across a client boundary is
the same class of incident as a member list doing it.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Count, Q
from django.utils import timezone
from django.http import FileResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from members.api.change_serializers import MemberChangeEventSerializer
from members.models import ChangeCategory, ChangeSeverity, MemberChangeEvent, ssn_fingerprint
from users.tenancy import resolve_client, scope_to_client
from edi.services.excel_generator import generate_excel

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _base_queryset(request):
    client = resolve_client(request)
    queryset = MemberChangeEvent.objects.filter(owner=request.user)
    return scope_to_client(queryset, client)


def _apply_filters(queryset, params):
    """
    Every filter the screen offers, applied server-side.

    Filtering in the browser was never an option here: a year of daily files on
    a six thousand member plan produces a change table with tens of thousands of
    rows, and shipping all of them so React can hide most of them is how a
    screen becomes unusable three months after go-live.
    """
    category = (params.get("category") or "").strip().upper()
    if category and category in ChangeCategory.values:
        queryset = queryset.filter(category=category)

    severity = (params.get("severity") or "").strip().upper()
    if severity and severity in ChangeSeverity.values:
        queryset = queryset.filter(severity=severity)

    field_name = (params.get("field") or "").strip()
    if field_name:
        queryset = queryset.filter(field_name=field_name)

    state = (params.get("state") or "open").strip().lower()
    if state == "open":
        queryset = queryset.filter(acknowledged_at__isnull=True)
    elif state == "acknowledged":
        queryset = queryset.filter(acknowledged_at__isnull=False)
    # state=all applies nothing.

    date_from = _parse_date(params.get("from"))
    if date_from:
        queryset = queryset.filter(current_file_date__gte=date_from)
    date_to = _parse_date(params.get("to"))
    if date_to:
        queryset = queryset.filter(current_file_date__lte=date_to)

    file_id = (params.get("file_id") or "").strip()
    if file_id.isdigit():
        queryset = queryset.filter(current_file_id=int(file_id))

    member_pk = (params.get("member") or "").strip()
    if member_pk.isdigit():
        queryset = queryset.filter(member_id=int(member_pk))

    search = (params.get("q") or "").strip()
    if search:
        # An SSN typed into the search box is matched on its fingerprint, never
        # by storing or comparing the digits. Anything else is a name or an
        # identifier.
        digits = "".join(ch for ch in search if ch.isdigit())
        if len(digits) == 9:
            queryset = queryset.filter(ssn_fingerprint=ssn_fingerprint(digits))
        elif len(digits) == 4 and digits == search.strip():
            queryset = queryset.filter(ssn_last4=digits)
        else:
            queryset = queryset.filter(
                Q(member_name__icontains=search)
                | Q(sponsor_member_id__icontains=search)
            )

    return queryset


class MemberChangeListView(APIView):
    """The change queue: every monitored difference, newest file first."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = _apply_filters(_base_queryset(request), request.query_params)
        queryset = queryset.select_related("previous_file", "current_file", "acknowledged_by")

        try:
            page_size = min(int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE)), MAX_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        total = queryset.count()
        start = (page - 1) * page_size
        rows = list(queryset[start:start + page_size])

        return Response(
            {
                "count": total,
                "page": page,
                "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size),
                "results": MemberChangeEventSerializer(rows, many=True).data,
            }
        )


class MemberChangeSummaryView(APIView):
    """
    The counts behind the tiles at the top of the change screen.

    Severity and category are counted over the open queue only, because that is
    the number somebody acts on. The all-time total is reported separately so
    the screen can say "eleven open, four hundred and twelve recorded" rather
    than implying the queue is the whole history.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        base = _base_queryset(request)
        open_queue = base.filter(acknowledged_at__isnull=True)

        by_severity = {
            row["severity"]: row["n"]
            for row in open_queue.values("severity").annotate(n=Count("id"))
        }
        by_category = {
            row["category"]: row["n"]
            for row in open_queue.values("category").annotate(n=Count("id"))
        }

        latest = (
            base.exclude(current_file_date__isnull=True)
            .order_by("-current_file_date")
            .values_list("current_file_date", flat=True)
            .first()
        )

        return Response(
            {
                "open": open_queue.count(),
                "total": base.count(),
                "acknowledged": base.filter(acknowledged_at__isnull=False).count(),
                "members_affected": open_queue.values("member_id").distinct().count(),
                "by_severity": {
                    value: by_severity.get(value, 0) for value in ChangeSeverity.values
                },
                "by_category": {
                    value: by_category.get(value, 0) for value in ChangeCategory.values
                },
                "latest_file_date": latest,
                # The vocabulary the filter dropdowns render, served from the
                # model rather than duplicated in the browser, so adding a
                # category is a backend change and not two.
                "categories": [
                    {"value": value, "label": label}
                    for value, label in ChangeCategory.choices
                ],
                "severities": [
                    {"value": value, "label": label}
                    for value, label in ChangeSeverity.choices
                ],
            }
        )


class MemberChangeDetailView(APIView):
    """
    Acknowledge one change, or reopen it.

    A note is optional but strongly encouraged, and the reason is auditability:
    "reviewed, member married, confirmed with sponsor" is the difference between
    a closed item and an unexplained one when somebody looks at this in a year.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        event = _base_queryset(request).filter(pk=pk).first()
        if event is None:
            return Response(
                {"detail": "No such change, or it belongs to another client."},
                status=status.HTTP_404_NOT_FOUND,
            )

        action = (request.data.get("action") or "acknowledge").strip().lower()
        note = (request.data.get("note") or "").strip()

        if action == "reopen":
            event.acknowledged_at = None
            event.acknowledged_by = None
            if note:
                event.note = note
            event.save(update_fields=["acknowledged_at", "acknowledged_by", "note"])
        else:
            event.acknowledged_at = timezone.now()
            event.acknowledged_by = request.user
            if note:
                event.note = note
            event.save(update_fields=["acknowledged_at", "acknowledged_by", "note"])

        return Response(MemberChangeEventSerializer(event).data)


class MemberChangeBulkAcknowledgeView(APIView):
    """
    Close a set of changes in one call.

    Present because the realistic use is a plan-wide event — a sponsor
    re-keying every address, a class code scheme changing — where three hundred
    rows are all the same fact and clicking each one is not review, it is
    typing. The note is applied to all of them so the reason survives.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return Response(
                {"detail": "Send a non-empty list of change ids as 'ids'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        note = (request.data.get("note") or "").strip()

        queryset = _base_queryset(request).filter(
            pk__in=[i for i in ids if str(i).isdigit()], acknowledged_at__isnull=True
        )
        updated = queryset.update(
            acknowledged_at=timezone.now(),
            acknowledged_by=request.user,
            **({"note": note} if note else {}),
        )
        return Response({"acknowledged": updated})


class MemberChangeExportView(APIView):
    """
    Export the current filtered change queue to an Excel workbook.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = _apply_filters(_base_queryset(request), request.query_params)
        queryset = queryset.select_related("previous_file", "current_file", "acknowledged_by")

        # Reuse the serializer to get the formatted displays (date formats, SSN masking, etc)
        # We do not paginate for export, as the user expects to download all matching rows.
        serialized_data = MemberChangeEventSerializer(queryset, many=True).data

        headers = [
            "Member Name",
            "SSN (Last 4)",
            "Member ID",
            "Member Type",
            "Category",
            "Field Changed",
            "Original Data",
            "Changed Data",
            "Previous File Name",
            "Current File Name",
            "Severity",
            "Status",
            "Acknowledged At",
            "Acknowledged By",
            "Note",
        ]

        rows = []
        for item in serialized_data:
            rows.append({
                "Member Name": item.get("member_name", ""),
                "SSN (Last 4)": item.get("ssn_last4", ""),
                "Member ID": item.get("member_id", ""),
                "Member Type": "Subscriber" if item.get("member_type") == "SUB" else "Dependant",
                "Category": item.get("category", ""),
                "Field Changed": item.get("field_label", ""),
                "Original Data": item.get("old_display", ""),
                "Changed Data": item.get("new_display", ""),
                "Previous File Name": item.get("previous_file_name", ""),
                "Current File Name": item.get("current_file_name", ""),
                "Severity": item.get("severity", ""),
                "Status": "Open" if item.get("is_open") else "Acknowledged",
                "Acknowledged At": item.get("acknowledged_at", ""),
                "Acknowledged By": item.get("acknowledged_by_name", ""),
                "Note": item.get("note", ""),
            })

        workbook_info = generate_excel(
            headers=headers,
            rows=rows,
            owner_id=request.user.id,
            source_name="Member_Changes_Export",
            sheet_title="Changes"
        )

        response = FileResponse(
            open(workbook_info.absolute_path, "rb"),
            as_attachment=True,
            filename=workbook_info.filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        if workbook_info.size_bytes:
            response["Content-Length"] = str(workbook_info.size_bytes)
        response["X-Content-Type-Options"] = "nosniff"
        
        return response

"""
Member lookup.

The defect this module existed to demonstrate: the search started from
Member.objects.filter(ssn_fingerprint=...) with no owner in the filter at all.
Any authenticated user who knew — or guessed — a nine digit number got back the
matching person's name, date of birth, plan, coverage history and every file
they had appeared in, regardless of who had uploaded it. On a system holding
PHI that is not a bug in a search screen, it is a disclosure.

Every queryset here now starts from the authenticated owner and, where the
deployment has clients configured, the selected client.
"""

import logging

from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from members.models import Member, ssn_fingerprint
from users.tenancy import resolve_client, scope_to_client

from .serializers import MemberSerializer

logger = logging.getLogger("edi.members.api")


def owned_members(request):
    """The only entry point into the Member table from the API."""
    client = resolve_client(request)
    return scope_to_client(Member.objects.filter(owner=request.user), client)


class MemberSearchView(APIView):
    """
    Find a member by SSN or member id, and report every valid file they appear in.

    Member history comes from the parsed X12 and is independent of the Excel
    mapping, so changing a column mapping cannot change what this returns.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        search_query = request.query_params.get("q", "").strip()
        if not search_query:
            return Response(
                {"detail": "Please provide an SSN or Member ID."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        clean_query = "".join(ch for ch in search_query if ch.isalnum())
        digits = "".join(ch for ch in search_query if ch.isdigit())

        base = owned_members(request).prefetch_related(
            "daily_statuses__uploaded_file", "eligibility_history__source_file"
        )

        members = base.none()
        if len(digits) == 9:
            fingerprint = ssn_fingerprint(digits)
            if fingerprint:
                members = base.filter(ssn_fingerprint=fingerprint)

        if not members.exists():
            members = base.filter(
                Q(ssn=digits) if digits else Q(pk__isnull=True)
            ) | base.filter(
                Q(member_id__iexact=search_query)
                | Q(member_id__iexact=clean_query)
                | Q(subscriber_number__iexact=search_query)
            )

        members = members.distinct().order_by("-created_at")

        if not members.exists():
            return Response(
                {"detail": "No member found with that SSN or Member ID."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = MemberSerializer(members, many=True)
        return Response(serializer.data)


class MemberDetailView(APIView):
    """One member, owner and client checked before anything is read."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        member = (
            owned_members(request)
            .prefetch_related(
                "daily_statuses__uploaded_file", "eligibility_history__source_file"
            )
            .filter(pk=pk)
            .first()
        )
        if not member:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(MemberSerializer(member).data)

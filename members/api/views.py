"""
Member lookup.

The defect this module existed to demonstrate: the search started from
Member.objects.filter(ssn_fingerprint=...) with no owner in the filter at all.
Any authenticated user who knew — or guessed — a nine digit number got back the
matching person's name, date of birth, plan, coverage history and every file
they had appeared in, regardless of who had uploaded it. On a system holding
PHI that is not a bug in a search screen, it is a disclosure. Every queryset
here now starts from the authenticated owner and, where the deployment has
clients configured, the selected client.

Part 13 adds the rest of what a member section has to do. Searching only
accepted an SSN or a member id, so "find me the Whitfields" was not a question
this screen could answer. An SSN was matched by fingerprint and then, if that
missed, by a raw equality against the plaintext column and an iexact against
member_id and subscriber_number — three lookups with different meanings ORed
together, which is how a search for a member id returns somebody whose SSN
happens to be the same digits.

The rules now:

  * An SSN is nine digits. A five digit or ten digit value in an SSN search is
    a validation error, not an empty result set, because the two are different
    problems and telling them apart is the difference between "no such member"
    and "you typed the wrong thing".
  * A digit string that is not nine long is still allowed to match a member id
    exactly, because sponsors do assign short numeric identifiers. It is only
    an SSN error when nothing matches it as an identifier.
  * Names match on first, last, or both across the pair.
  * Results resolve through the Subscriber and Dependant master tables, so what
    comes back reflects the de-duplicated roster rather than one row per file.
"""

import logging

from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from edi.services.ssn import clean_ssn, normalize_ssn
from members.models import Dependant, Member, Subscriber, ssn_fingerprint
from users.tenancy import resolve_client, scope_to_client

from .serializers import DependantSerializer, MemberSerializer, SubscriberSerializer

logger = logging.getLogger("edi.members.api")

MAX_RESULTS = 50


def owned_members(request):
    """The only entry point into the Member table from the API."""
    client = resolve_client(request)
    return scope_to_client(Member.objects.filter(owner=request.user), client)


def owned_masters(request, model):
    client = resolve_client(request)
    return scope_to_client(model.objects.filter(owner=request.user), client)


def _prefetched(queryset):
    return queryset.select_related("subscriber").prefetch_related(
        "daily_statuses__uploaded_file",
        "eligibility_history__source_file",
        "subscriber_record__enrollments__source_file",
        "dependant_record__enrollments__source_file",
    )


def _classify(query: str) -> str:
    """
    What kind of thing has the user typed.

    Deliberately conservative: only an unambiguous nine digit string is treated
    as an SSN without being asked. Everything else is searched as an identifier
    and a name, and the caller can force the interpretation with ?field=.
    """
    stripped = query.strip()
    digits = clean_ssn(stripped)
    if digits and len(digits) == len(stripped.replace("-", "").replace(" ", "")):
        return "SSN" if len(digits) == 9 else "NUMERIC"
    return "TEXT"


class MemberSearchView(APIView):
    """
    Find a member by SSN, member id or name, with the files they appear in.

    Member history comes from the parsed X12 and is independent of the Excel
    mapping, so changing a column mapping cannot change what this returns.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        search_query = request.query_params.get("q", "").strip()
        field = (request.query_params.get("field") or "AUTO").upper()

        if not search_query:
            return Response(
                {"detail": "Please provide an SSN, Member ID or name to search for."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kind = field if field in ("SSN", "MEMBER_ID", "FIRST_NAME", "LAST_NAME") else _classify(search_query)

        # -----------------------------------------------------------------
        # An SSN search is validated before it is run.
        #
        # Returning "no member found" for 12345 is a lie by omission: there is
        # no member found because 12345 cannot be an SSN, and the user needs to
        # know which of those two things happened.
        # -----------------------------------------------------------------
        if kind == "SSN":
            digits, error = normalize_ssn(search_query)
            if error:
                return Response(
                    {
                        "detail": error,
                        "field": "ssn",
                        "expected": "Nine digits, with or without dashes.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            members = self._by_ssn(request, digits)
            if not members:
                return Response(
                    {"detail": "No member found with that SSN."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(self._serialise(members))

        members = self._by_identifier_or_name(request, search_query, kind)

        if not members and kind == "NUMERIC":
            # It was all digits, it matched no identifier, and it is not nine
            # digits long. The most useful thing to say is why it cannot be an
            # SSN either.
            _, error = normalize_ssn(search_query)
            return Response(
                {
                    "detail": (
                        error
                        or "No member found with that identifier."
                    )
                    + " No member ID matches it either.",
                    "field": "ssn",
                    "expected": "Nine digits for an SSN, or an exact Member ID.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not members:
            return Response(
                {"detail": "No member found with that SSN, Member ID or name."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(self._serialise(members))

    # -- lookups ------------------------------------------------------------

    def _by_ssn(self, request, digits):
        """
        Resolve nine digits through the master tables first.

        The master tables carry a unique constraint on SSN, so at most one
        subscriber and one dependant can hold a given number for a tenant. That
        is what makes "do not return duplicate records" a property of the
        database rather than of a distinct() call somebody has to remember.
        """
        fingerprint = ssn_fingerprint(digits)
        member_ids = set()

        for model in (Subscriber, Dependant):
            for record in owned_masters(request, model).filter(
                Q(ssn=digits) | Q(ssn_fingerprint=fingerprint)
            ):
                if record.source_member_id:
                    member_ids.add(record.source_member_id)

        base = _prefetched(owned_members(request))
        if member_ids:
            return list(base.filter(pk__in=member_ids).order_by("member_type", "id"))

        # A member synced before the master tables existed, or one whose
        # projection failed. Fall back to the fingerprint on Member itself.
        return list(
            base.filter(ssn_fingerprint=fingerprint).order_by("member_type", "id")
        )

    def _by_identifier_or_name(self, request, query, kind):
        base = _prefetched(owned_members(request))
        criteria = Q(pk__isnull=True)  # matches nothing; OR-ed into below

        if kind in ("MEMBER_ID", "NUMERIC", "TEXT", "AUTO"):
            criteria |= Q(member_id__iexact=query) | Q(subscriber_number__iexact=query)

        if kind == "FIRST_NAME":
            criteria |= Q(first_name__icontains=query)
        elif kind == "LAST_NAME":
            criteria |= Q(last_name__icontains=query)
        elif kind in ("TEXT", "AUTO"):
            criteria |= (
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(middle_name__icontains=query)
                | Q(member_id__icontains=query)
            )
            parts = [part for part in query.split() if part]
            if len(parts) > 1:
                # "john smith" and "smith john" are the same request.
                criteria |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
                criteria |= Q(first_name__icontains=parts[-1]) & Q(
                    last_name__icontains=parts[0]
                )

        return list(
            base.filter(criteria)
            .distinct()
            .order_by("member_type", "last_name", "first_name", "id")[:MAX_RESULTS]
        )

    def _serialise(self, members):
        return MemberSerializer(self._expand_families(members), many=True).data

    def _expand_families(self, members):
        """
        Widen a hit to the family it belongs to.

        The Members screen shows a subscriber together with their dependants,
        selected by radio button, so a search that matches one person has to
        return everyone who shares that person's subscriber. A dependant hit
        pulls in its subscriber and siblings; a subscriber hit pulls in its
        dependants. Ordering puts each subscriber first, then that family's
        dependants, so the screen can group without re-sorting.
        """
        by_id = {member.id: member for member in members}
        subscriber_ids = set()
        for member in members:
            if member.member_type == "SUB":
                subscriber_ids.add(member.id)
            elif member.subscriber_id:
                subscriber_ids.add(member.subscriber_id)

        if subscriber_ids:
            request = self.request
            family = _prefetched(owned_members(request)).filter(
                Q(pk__in=subscriber_ids) | Q(subscriber_id__in=subscriber_ids)
            )
            for member in family:
                by_id.setdefault(member.id, member)

        def family_key(member):
            head = member.id if member.member_type == "SUB" else (member.subscriber_id or 0)
            # Subscriber first inside the family, then dependants by name.
            return (head, member.member_type != "SUB", member.last_name, member.first_name, member.id)

        return sorted(by_id.values(), key=family_key)


class MemberDetailView(APIView):
    """One member, owner and client checked before anything is read."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        member = _prefetched(owned_members(request)).filter(pk=pk).first()
        if not member:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(MemberSerializer(member).data)


class SubscriberListView(APIView):
    """
    The subscriber master table, on its own.

    Part 3 asks for the two roles to be separated at the database level. This
    is the endpoint that lets anyone verify it: what comes back is one row per
    person, never a dependant, whatever the file said.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = owned_masters(request, Subscriber).prefetch_related(
            "dependants", "enrollments__source_file"
        )
        query = (request.query_params.get("q") or "").strip()
        if query:
            queryset = queryset.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(member_id__icontains=query)
            )
        ssn = (request.query_params.get("ssn") or "").strip()
        if ssn:
            digits, error = normalize_ssn(ssn)
            if error:
                return Response(
                    {"detail": error, "field": "ssn"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(ssn=digits)

        queryset = queryset.order_by("last_name", "first_name")[:MAX_RESULTS]
        return Response(
            {
                "count": len(queryset),
                "results": SubscriberSerializer(queryset, many=True).data,
            }
        )


class DependantListView(APIView):
    """The dependant master table, on its own. Never contains a subscriber."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = owned_masters(request, Dependant).select_related(
            "subscriber"
        ).prefetch_related("enrollments__source_file")

        query = (request.query_params.get("q") or "").strip()
        if query:
            queryset = queryset.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(member_id__icontains=query)
            )
        ssn = (request.query_params.get("ssn") or "").strip()
        if ssn:
            digits, error = normalize_ssn(ssn)
            if error:
                return Response(
                    {"detail": error, "field": "ssn"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(ssn=digits)

        subscriber_id = request.query_params.get("subscriber_id")
        if subscriber_id:
            queryset = queryset.filter(subscriber_id=subscriber_id)

        queryset = queryset.order_by("last_name", "first_name")[:MAX_RESULTS]
        return Response(
            {
                "count": len(queryset),
                "results": DependantSerializer(queryset, many=True).data,
            }
        )

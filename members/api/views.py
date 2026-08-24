import logging
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from members.models import Member, ssn_fingerprint
from .serializers import MemberSerializer

logger = logging.getLogger("edi.members.api")

class MemberSearchView(APIView):
    """
    Search for a member by SSN.
    Returns member demographics and history across all files.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        search_query = request.query_params.get("q", "").strip()
        if not search_query:
            return Response(
                {"detail": "Please provide an SSN or Member ID."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Strip dashes if they typed them (for SSN)
        clean_query = search_query.replace("-", "")

        fingerprint = ssn_fingerprint(clean_query)

        # We allow searching by fingerprint, plaintext SSN, or member_id
        members = Member.objects.filter(
            ssn_fingerprint=fingerprint
        ).prefetch_related('daily_statuses__uploaded_file', 'eligibility_history__source_file').order_by("-created_at")

        if not members.exists():
            from django.db.models import Q
            members = Member.objects.filter(
                Q(ssn=clean_query) | Q(member_id=search_query) | Q(member_id=clean_query)
            ).prefetch_related('daily_statuses__uploaded_file', 'eligibility_history__source_file').order_by("-created_at")

        if not members.exists():
            return Response(
                {"detail": "No member found with that SSN or Member ID."}, 
                status=status.HTTP_404_NOT_FOUND
            )

        # An SSN might belong to a subscriber and sometimes multiple records depending on file state,
        # but typically just one active Member object.
        serializer = MemberSerializer(members, many=True)
        return Response(serializer.data)

"""
Roster scale regression.

GET /api/members/roster/ raised sqlite3.OperationalError: too many SQL
variables once the member table passed SQLite's bound-parameter cap (999 on
the builds most distributions ship). The cause was prefetch_related on the
roster queryset: Django's prefetch collects every member primary key the outer
query returned and issues WHERE member_id IN (?, ?, ... x N) with one bound
variable per key. The fix loads the spans through a subquery instead —
spans_for() in members.services.presence — so the ids never leave the
database. This test seeds comfortably past the cap and asserts the endpoint
answers, pages, and filters by file_date, which is the exact request shape
that failed.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from files.models import ProcessingStatus, UploadedFile
from members.models import Member, MemberDailyStatus, MemberEligibilityHistory

MEMBER_COUNT = 1200  # past the 999-variable cap with room to spare


class RosterScaleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user("staff", password="pw")
        cls.staff.is_staff = True
        cls.staff.save(update_fields=["is_staff"])

        cls.upload = UploadedFile.objects.create(
            owner=cls.staff,
            original_filename="big.x12",
            file_size_bytes=1,
            content_sha256="0" * 64,
            processing_status=ProcessingStatus.VALIDATED,
            file_date=date(2025, 7, 7),
            member_loop_count=MEMBER_COUNT,
        )

        members = Member.objects.bulk_create(
            Member(
                owner=cls.staff,
                member_type="SUB",
                relationship_code="18",
                member_id="M{n:06d}".format(n=n),
                first_name="FIRST{n}".format(n=n),
                last_name="LAST{n:06d}".format(n=n),
            )
            for n in range(MEMBER_COUNT)
        )
        MemberEligibilityHistory.objects.bulk_create(
            MemberEligibilityHistory(
                member=member,
                insurance_line_code="HLT",
                effective_date=date(2025, 1, 1),
                termination_date=None,
                source_file=cls.upload,
            )
            for member in members
        )
        MemberDailyStatus.objects.bulk_create(
            MemberDailyStatus(
                member=member,
                uploaded_file=cls.upload,
                status_date=cls.upload.file_date,
                change_type="ADDED",
            )
            for member in members
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def test_roster_answers_past_the_bound_variable_cap(self):
        response = self.client.get(
            "/api/members/roster/", {"page": 1, "page_size": 25}
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["count"], MEMBER_COUNT)
        self.assertEqual(len(body["results"]), 25)
        self.assertEqual(body["counts"]["total"], MEMBER_COUNT)
        self.assertEqual(body["counts"]["present"], MEMBER_COUNT)

    def test_pagination_reaches_the_last_page(self):
        response = self.client.get(
            "/api/members/roster/", {"page": 48, "page_size": 25}
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["page"], 48)
        self.assertEqual(len(body["results"]), 25)
        # A different slice, not the first page again.
        self.assertNotEqual(
            body["results"][0]["last_name"], "LAST000000"
        )

    def test_file_date_filter_still_works(self):
        response = self.client.get(
            "/api/members/roster/",
            {"file_date": "2025-07-07", "page": 1, "page_size": 25},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["file_date"], "2025-07-07")
        self.assertEqual(body["counts"]["in_file"], MEMBER_COUNT)

    def test_rows_carry_their_spans(self):
        """The spans still reach the response after the prefetch removal."""
        response = self.client.get(
            "/api/members/roster/", {"page": 1, "page_size": 5}
        )
        row = response.json()["results"][0]
        self.assertEqual(row["effective_date"], "2025-01-01")
        self.assertIsNone(row["termination_date"])
        self.assertEqual(row["presence"], "PRESENT")

    def test_absent_date_reports_an_empty_roster_cleanly(self):
        response = self.client.get(
            "/api/members/roster/",
            {"file_date": "2025-08-08", "page": 1, "page_size": 25},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["counts"]["total"], 0)

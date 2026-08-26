"""
Tests for the three things this change set claims.

The claims are: an unchanged member is not written again, a member whose
details move between two files produces reviewable change rows naming both
files, and the member card always has an identifier to show. Each is asserted
against behaviour rather than against an implementation detail, so a future
rewrite of the fast path is free to work differently as long as it still holds.
"""

from __future__ import annotations

import datetime

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient
from django.test import TestCase, override_settings

from edi.services.digest import loop_digest
from edi.services.ingest import sync_uploaded_file
from files.models import UploadedFile
from members.api.serializers import MemberSerializer
from members.models import (
    ChangeSeverity,
    Member,
    MemberChangeEvent,
    MemberDailyStatus,
    Subscriber,
)

MEDIA = "/tmp/834-change-tests"


def build_834(file_date, entries, control=1):
    """entries: (is_sub, rel, mtc, last, first, ssn, dob, gender, plan, eff, term, ref0f, city)."""
    stamp = file_date.strftime("%Y%m%d")
    head = [
        "ISA*00*          *00*          *ZZ*ONESMARTER     *ZZ*ABCHEALTHPLAN  *"
        "{d}*1200*^*00501*{c:09d}*0*P*:".format(d=file_date.strftime("%y%m%d"), c=control),
        "GS*BE*ONESMARTER*ABCHEALTHPLAN*{d}*1200*{c}*X*005010X220A1".format(d=stamp, c=control),
    ]
    body = [
        "ST*834*0001*005010X220A1",
        "BGN*00*{c:08d}*{d}*1200****4".format(c=control, d=stamp),
        "REF*38*GRP1001",
        "N1*P5*TEST SPONSOR*FI*351234567",
    ]
    for entry in entries:
        (is_sub, rel, mtc, last, first, ssn, dob, gender, plan, eff, term, ref0f, city) = entry
        body.append("INS*{s}*{r}*{m}*XN*A***FT".format(s="Y" if is_sub else "N", r=rel, m=mtc))
        body.append("REF*0F*{r}".format(r=ref0f))
        body.append("REF*1L*GRP1001")
        body.append("NM1*IL*1*{l}*{f}****34*{s}".format(l=last, f=first, s=ssn))
        body.append("N3*100 MAIN ST")
        body.append("N4*{c}*OH*45402".format(c=city))
        body.append("DMG*D8*{d}*{g}".format(d=dob, g=gender))
        body.append("HD*{m}**HLT*{p}*IND".format(m=mtc, p=plan))
        body.append("DTP*348*D8*{e}".format(e=eff))
        if term:
            body.append("DTP*349*D8*{t}".format(t=term))
    body.append("SE*{n}*0001".format(n=len(body) + 1))
    segments = head + body + ["GE*1*{c}".format(c=control), "IEA*1*{c:09d}".format(c=control)]
    return ("~\n".join(segments) + "~\n").encode()


SAM = (True, "18", "030", "STAY", "SAM", "111000111", "19800101", "M",
       "PPO-GOLD", "20240101", None, "SUB0001", "DAYTON")
LEE = (True, "18", "030", "LEAVE", "LEE", "222000222", "19850101", "F",
       "PPO-SILVER", "20240101", None, "SUB0002", "DAYTON")


@override_settings(MEDIA_ROOT=MEDIA)
class ChangeMonitorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "ops", password="pw12345678", is_staff=True
        )

    def _load(self, entries, when, name, control=1):
        payload = build_834(when, entries, control=control)
        record = UploadedFile(
            owner=self.user,
            original_filename=name,
            file_size_bytes=len(payload),
            content_sha256=name,
            file_date=when,
        )
        record.stored_file.save(name, SimpleUploadedFile(name, payload), save=False)
        record.save()
        return record, sync_uploaded_file(record, self.user)

    # -- de-duplication ------------------------------------------------

    def test_an_unchanged_member_is_not_stored_again(self):
        """
        The plain reading of the requirement: the same person in two files is
        one row, not two, and the second file leaves that row alone.
        """
        self._load([SAM, LEE], datetime.date(2024, 4, 1), "day1.txt")
        self.assertEqual(Member.objects.count(), 2)
        self.assertEqual(Subscriber.objects.count(), 2)

        before = {m.pk: m.updated_at for m in Member.objects.all()}

        _record, summary = self._load(
            [SAM, LEE], datetime.date(2024, 4, 10), "day10.txt", control=2
        )

        self.assertEqual(Member.objects.count(), 2, "a re-sent roster created new people")
        self.assertEqual(Subscriber.objects.count(), 2)
        self.assertEqual(summary["skipped_unchanged"], 2)
        self.assertEqual(summary["changes_recorded"], 0)

        # Not merely idempotent - untouched. If the rows had been rewritten to
        # the same values, updated_at would have moved.
        for member in Member.objects.all():
            self.assertEqual(member.updated_at, before[member.pk])

    def test_presence_in_the_second_file_is_still_recorded(self):
        """
        Skipping the write must not skip the history. "Do not store again" is
        about the person, not about the fact that they appeared.
        """
        self._load([SAM], datetime.date(2024, 4, 1), "p1.txt")
        self._load([SAM], datetime.date(2024, 4, 10), "p2.txt", control=2)

        member = Member.objects.get(ssn="111000111")
        self.assertEqual(member.daily_statuses.count(), 2)
        self.assertEqual(
            set(member.daily_statuses.values_list("change_type", flat=True)),
            {MemberDailyStatus.ChangeType.ADDED, MemberDailyStatus.ChangeType.UNCHANGED},
        )
        self.assertEqual(member.subscriber_record.enrollments.count(), 2)

    def test_reuploading_the_same_file_writes_nothing_twice(self):
        record, _ = self._load([SAM], datetime.date(2024, 4, 1), "r1.txt")
        sync_uploaded_file(record, self.user)
        member = Member.objects.get(ssn="111000111")
        self.assertEqual(member.daily_statuses.count(), 1)
        self.assertEqual(member.subscriber_record.enrollments.count(), 1)

    # -- change detection ----------------------------------------------

    def test_a_change_between_two_files_is_recorded_with_both_sides(self):
        """The stated requirement, in full: same SSN, day 1 and day 10, changed."""
        self._load([SAM], datetime.date(2024, 4, 1), "c1.txt")

        moved = list(SAM)
        moved[8] = "HDHP-CORE"   # plan
        moved[12] = "COLUMBUS"   # city
        self._load([tuple(moved)], datetime.date(2024, 4, 10), "c2.txt", control=2)

        events = {e.field_name: e for e in MemberChangeEvent.objects.all()}
        self.assertIn("plan_code", events)
        self.assertIn("city", events)

        plan = events["plan_code"]
        self.assertEqual(plan.old_value, "PPO-GOLD")
        self.assertEqual(plan.new_value, "HDHP-CORE")
        self.assertEqual(plan.severity, ChangeSeverity.CRITICAL)
        self.assertEqual(plan.previous_file.original_filename, "c1.txt")
        self.assertEqual(plan.current_file.original_filename, "c2.txt")
        self.assertEqual(plan.previous_file_date, datetime.date(2024, 4, 1))
        self.assertEqual(plan.current_file_date, datetime.date(2024, 4, 10))
        self.assertTrue(plan.is_open)

        # A city move is real but not urgent; severity has to separate them or
        # the queue is just a list.
        self.assertEqual(events["city"].severity, ChangeSeverity.INFO)

    def test_a_first_appearance_is_not_a_change(self):
        self._load([SAM, LEE], datetime.date(2024, 4, 1), "f1.txt")
        self.assertEqual(MemberChangeEvent.objects.count(), 0)

    def test_a_blank_field_becoming_populated_is_not_a_change(self):
        no_city = list(SAM)
        no_city[12] = ""
        self._load([tuple(no_city)], datetime.date(2024, 4, 1), "b1.txt")
        self._load([SAM], datetime.date(2024, 4, 10), "b2.txt", control=2)
        self.assertFalse(
            MemberChangeEvent.objects.filter(field_name="city").exists(),
            "the sponsor filling a gap was reported as a change",
        )

    def test_termination_is_recorded_as_a_critical_change(self):
        self._load([SAM], datetime.date(2024, 4, 1), "t1.txt")
        ended = list(SAM)
        ended[2] = "024"
        ended[10] = "20240409"
        self._load([tuple(ended)], datetime.date(2024, 4, 10), "t2.txt", control=2)

        event = MemberChangeEvent.objects.get(field_name="coverage_termination_date")
        self.assertEqual(event.severity, ChangeSeverity.CRITICAL)
        self.assertEqual(event.new_value, "2024-04-09")

    def test_rerunning_a_file_does_not_duplicate_its_changes(self):
        self._load([SAM], datetime.date(2024, 4, 1), "d1.txt")
        moved = list(SAM)
        moved[8] = "HDHP-CORE"
        record, _ = self._load([tuple(moved)], datetime.date(2024, 4, 10), "d2.txt", control=2)
        first = MemberChangeEvent.objects.count()
        sync_uploaded_file(record, self.user)
        self.assertEqual(MemberChangeEvent.objects.count(), first)

    # -- the digest ----------------------------------------------------

    def test_the_digest_ignores_cosmetic_differences(self):
        base = {"first_name": "SAM", "last_name": "STAY", "coverages": []}
        spaced = {"first_name": " SAM ", "last_name": "STAY", "coverages": []}
        self.assertEqual(loop_digest(base), loop_digest(spaced))

    def test_the_digest_moves_when_a_stored_field_moves(self):
        base = {"first_name": "SAM", "plan_code": "PPO-GOLD", "coverages": []}
        moved = {"first_name": "SAM", "plan_code": "HDHP-CORE", "coverages": []}
        self.assertNotEqual(loop_digest(base), loop_digest(moved))

    def test_the_digest_ignores_coverage_line_order(self):
        one = {
            "coverages": [
                {"insurance_line_code": "HLT", "plan_code": "A"},
                {"insurance_line_code": "DEN", "plan_code": "B"},
            ]
        }
        other = {
            "coverages": [
                {"insurance_line_code": "DEN", "plan_code": "B"},
                {"insurance_line_code": "HLT", "plan_code": "A"},
            ]
        }
        self.assertEqual(loop_digest(one), loop_digest(other))

    # -- identity safety -----------------------------------------------

    def test_a_shared_subscriber_number_does_not_merge_two_people(self):
        """
        The trap the member-id fallback walked into. Two distinct subscribers
        that happen to share a REF*0F are two people, and any change that makes
        REF*0F an identity key silently makes them one.
        """
        shared_lee = list(LEE)
        shared_lee[11] = "SUB0001"
        self._load([SAM, tuple(shared_lee)], datetime.date(2024, 4, 1), "m1.txt")
        self.assertEqual(Member.objects.count(), 2)
        self.assertEqual(Subscriber.objects.count(), 2)

    # -- the member card -----------------------------------------------

    def test_the_card_always_has_an_identifier_to_show(self):
        self._load([SAM], datetime.date(2024, 4, 1), "i1.txt")
        member = Member.objects.get(ssn="111000111")
        data = MemberSerializer(member).data

        self.assertEqual(member.member_id, "", "REF*0F leaked into the identity column")
        self.assertEqual(data["display_member_id"], "SUB0001")
        self.assertEqual(data["member_id_source"], "Subscriber number (REF*0F)")

    def test_the_card_carries_the_full_detail_set(self):
        self._load([SAM], datetime.date(2024, 4, 1), "i2.txt")
        data = MemberSerializer(Member.objects.get(ssn="111000111")).data
        for field in (
            "display_member_id", "member_id_source", "middle_name", "name_suffix",
            "address1", "city", "state", "postal_code", "phone_display", "email",
            "local", "benefit_status_code", "first_seen", "last_seen", "recent_changes",
        ):
            self.assertIn(field, data, field)
        self.assertEqual(data["city"], "DAYTON")
        self.assertEqual(data["first_seen"]["name"], "i2.txt")

    def test_the_card_never_emits_a_full_ssn(self):
        self._load([SAM], datetime.date(2024, 4, 1), "i3.txt")
        data = MemberSerializer(Member.objects.get(ssn="111000111")).data
        self.assertNotIn("111000111", str(data))
        self.assertEqual(data["masked_ssn"], "XXX-XX-0111")


@override_settings(MEDIA_ROOT=MEDIA)
class ChangeApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "api", password="pw12345678", is_staff=True
        )
        payload = build_834(datetime.date(2024, 4, 1), [SAM])
        record = UploadedFile(
            owner=self.user, original_filename="a1.txt", file_size_bytes=len(payload),
            content_sha256="a1", file_date=datetime.date(2024, 4, 1),
        )
        record.stored_file.save("a1.txt", SimpleUploadedFile("a1.txt", payload), save=False)
        record.save()
        sync_uploaded_file(record, self.user)

        moved = list(SAM)
        moved[8] = "HDHP-CORE"
        payload = build_834(datetime.date(2024, 4, 10), [tuple(moved)], control=2)
        record = UploadedFile(
            owner=self.user, original_filename="a2.txt", file_size_bytes=len(payload),
            content_sha256="a2", file_date=datetime.date(2024, 4, 10),
        )
        record.stored_file.save("a2.txt", SimpleUploadedFile("a2.txt", payload), save=False)
        record.save()
        sync_uploaded_file(record, self.user)

        self.http = HttpClient()
        self.http.login(username="api", password="pw12345678")

    def test_anonymous_is_refused(self):
        self.assertEqual(HttpClient().get("/api/members/changes/").status_code, 403)

    def test_the_queue_lists_open_changes_by_default(self):
        body = self.http.get("/api/members/changes/").json()
        self.assertGreaterEqual(body["count"], 1)
        row = body["results"][0]
        self.assertTrue(row["is_open"])
        self.assertIn("field_label", row)
        self.assertIn("current_file_name", row)

    def test_dates_are_rendered_for_display(self):
        body = self.http.get("/api/members/changes/", {"field": "plan_code"}).json()
        self.assertEqual(body["results"][0]["current_file_date_display"], "04-10-2024")

    def test_filters_narrow_the_queue(self):
        self.assertEqual(
            self.http.get("/api/members/changes/", {"severity": "INFO"}).json()["count"], 0
        )
        self.assertGreaterEqual(
            self.http.get("/api/members/changes/", {"severity": "CRITICAL"}).json()["count"], 1
        )

    def test_summary_counts_the_open_queue(self):
        body = self.http.get("/api/members/changes/summary/").json()
        self.assertGreaterEqual(body["open"], 1)
        self.assertEqual(body["acknowledged"], 0)
        self.assertEqual(body["members_affected"], 1)
        self.assertTrue(body["categories"])

    def test_acknowledging_closes_a_change_and_records_who(self):
        event_id = self.http.get("/api/members/changes/").json()["results"][0]["id"]
        response = self.http.post(
            "/api/members/changes/{pk}/".format(pk=event_id),
            data={"note": "confirmed with sponsor"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["is_open"])
        self.assertEqual(body["note"], "confirmed with sponsor")
        self.assertEqual(body["acknowledged_by_name"], "api")
        self.assertEqual(self.http.get("/api/members/changes/").json()["count"], 0)

    def test_bulk_acknowledge(self):
        ids = [r["id"] for r in self.http.get("/api/members/changes/").json()["results"]]
        response = self.http.post(
            "/api/members/changes/acknowledge/",
            data={"ids": ids, "note": "plan-wide migration"},
            content_type="application/json",
        )
        self.assertEqual(response.json()["acknowledged"], len(ids))

    def test_another_users_changes_are_invisible(self):
        get_user_model().objects.create_user("nosy", password="pw12345678")
        other = HttpClient()
        other.login(username="nosy", password="pw12345678")
        self.assertEqual(other.get("/api/members/changes/").json()["count"], 0)

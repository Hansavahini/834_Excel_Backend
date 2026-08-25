"""
Regression tests for the integration work.

Each test corresponds to a defect that was found and fixed, or to a behaviour of
the Info section that is easy to break by accident. The presence tests guard the
distinction between "appeared in the file" and "was covered on the date", which
is the thing about that screen most likely to be simplified back into a bug.
"""

import datetime
import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from edi.services.ingest import sync_uploaded_file
from edi.services.loop_extractor import StreamingParsedFile
from edi.services.parser import EDI834Parser
from edi.services.x12_834_to_db import convert_834_to_member
from files.models import UploadedFile
from members.models import (
    Member,
    MemberEligibilityHistory,
    normalize_ssn,
    ssn_fingerprint,
)

MEDIA = tempfile.mkdtemp(prefix="edi-integration-media-")


def build_834(file_date, entries, control=1):
    """entries: (is_sub, rel, mtc, last, first, ssn, dob, gender, plan, eff, term)."""
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
    for is_sub, rel, mtc, last, first, ssn, dob, gender, plan, eff, term in entries:
        body.append("INS*{s}*{r}*{m}*XN*A***FT".format(s="Y" if is_sub else "N", r=rel, m=mtc))
        body.append("REF*0F*SUB0001")
        body.append("REF*1L*GRP1001")
        body.append("NM1*IL*1*{l}*{f}****34*{s}".format(l=last, f=first, s=ssn))
        body.append("DMG*D8*{d}*{g}".format(d=dob, g=gender))
        body.append("HD*{m}**HLT*{p}*IND".format(m=mtc, p=plan))
        body.append("DTP*348*D8*{e}".format(e=eff))
        if term:
            body.append("DTP*349*D8*{t}".format(t=term))
    body.append("SE*{n}*0001".format(n=len(body) + 1))
    segments = head + body + ["GE*1*{c}".format(c=control), "IEA*1*{c:09d}".format(c=control)]
    return ("~\n".join(segments) + "~\n").encode()


@override_settings(MEDIA_ROOT=MEDIA)
class SSNNormalisationTests(TestCase):
    def test_punctuation_does_not_create_a_second_identity(self):
        self.assertEqual(normalize_ssn("123-45-6789"), "123456789")
        self.assertEqual(normalize_ssn("123 45 6789"), "123456789")
        self.assertEqual(ssn_fingerprint("123-45-6789"), ssn_fingerprint("123456789"))

    def test_blank_ssn_fingerprints_to_blank(self):
        self.assertEqual(ssn_fingerprint(""), "")
        self.assertEqual(ssn_fingerprint(None), "")

    def test_member_save_normalises(self):
        user = get_user_model().objects.create_user("norm", password="pw12345678")
        member = Member.objects.create(
            owner=user, member_type="SUB", first_name="A", last_name="B", ssn="123-45-6789"
        )
        member.refresh_from_db()
        self.assertEqual(member.ssn, "123456789")
        self.assertEqual(member.ssn_fingerprint, ssn_fingerprint("123456789"))


@override_settings(MEDIA_ROOT=MEDIA)
class CoverageLineTests(TestCase):
    def test_two_hd_segments_produce_two_coverage_lines(self):
        """The old converter kept only the last HD, losing dental or vision."""
        payload = (
            "ISA*00*          *00*          *ZZ*A              *ZZ*B              *"
            "240401*1200*^*00501*000000001*0*P*:~\n"
            "GS*BE*A*B*20240401*1200*1*X*005010X220A1~\n"
            "ST*834*0001*005010X220A1~\n"
            "INS*Y*18*021*XN*A***FT~\n"
            "NM1*IL*1*DOE*JANE****34*111223333~\n"
            "DMG*D8*19800101*F~\n"
            "HD*021**HLT*PPO-GOLD*IND~\n"
            "DTP*348*D8*20240101~\n"
            "HD*021**DEN*DENTAL-BASE*IND~\n"
            "DTP*348*D8*20240201~\n"
            "SE*8*0001~\nGE*1*1~\nIEA*1*000000001~\n"
        )
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write(payload)
        handle.close()
        try:
            loops = list(StreamingParsedFile(EDI834Parser(handle.name).iter_segments()))
            parsed = convert_834_to_member(loops[0])
        finally:
            os.unlink(handle.name)

        lines = {c["insurance_line_code"] for c in parsed["coverages"]}
        self.assertEqual(lines, {"HLT", "DEN"})
        dental = [c for c in parsed["coverages"] if c["insurance_line_code"] == "DEN"][0]
        self.assertEqual(dental["effective_date"], datetime.date(2024, 2, 1))


@override_settings(MEDIA_ROOT=MEDIA)
class IngestTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ing", password="pw12345678")

    def _upload(self, content, name, when=None):
        record = UploadedFile(
            owner=self.user,
            original_filename=name,
            file_size_bytes=len(content),
            content_sha256=name,
            file_date=when,
        )
        record.stored_file.save(name, SimpleUploadedFile(name, content), save=False)
        record.save()
        return record

    def test_all_parsed_fields_reach_the_database(self):
        content = build_834(
            datetime.date(2024, 4, 1),
            [(True, "18", "021", "DOE", "JANE", "111223333", "19800101", "F",
              "PPO-GOLD", "20240101", None)],
        )
        record = self._upload(content, "a.txt", datetime.date(2024, 4, 1))
        summary = sync_uploaded_file(record, self.user)

        self.assertEqual(summary["failed"], 0)
        member = Member.objects.get(ssn="111223333")
        # REF*0F used to be parsed and then discarded on create.
        self.assertEqual(member.subscriber_number, "SUB0001")
        self.assertEqual(member.group_number, "GRP1001")
        self.assertEqual(member.plan_code, "PPO-GOLD")
        self.assertEqual(member.coverage_status, "ACTIVE")

    def test_reupload_does_not_lose_the_member(self):
        """The unique span constraint used to raise inside the atomic block."""
        content = build_834(
            datetime.date(2024, 4, 1),
            [(True, "18", "030", "DOE", "JANE", "111223333", "19800101", "F",
              "PPO-GOLD", "20240101", None)],
        )
        first = self._upload(content, "b1.txt", datetime.date(2024, 4, 1))
        sync_uploaded_file(first, self.user)

        second = self._upload(content, "b2.txt", datetime.date(2024, 4, 2))
        summary = sync_uploaded_file(second, self.user)

        self.assertEqual(summary["failed"], 0)
        self.assertEqual(Member.objects.filter(ssn="111223333").count(), 1)
        self.assertEqual(MemberEligibilityHistory.objects.count(), 1)

    def test_dtp349_terminates_without_an_024_code(self):
        """A 001 change carrying an end date must still close the span."""
        opening = build_834(
            datetime.date(2024, 4, 1),
            [(True, "18", "021", "ROE", "SAM", "222334444", "19750505", "M",
              "PPO-GOLD", "20240101", None)],
        )
        sync_uploaded_file(self._upload(opening, "c1.txt", datetime.date(2024, 4, 1)), self.user)

        closing = build_834(
            datetime.date(2024, 4, 15),
            [(True, "18", "001", "ROE", "SAM", "222334444", "19750505", "M",
              "PPO-GOLD", "20240101", "20240414")],
            control=2,
        )
        sync_uploaded_file(self._upload(closing, "c2.txt", datetime.date(2024, 4, 15)), self.user)

        span = MemberEligibilityHistory.objects.get(member__ssn="222334444")
        self.assertEqual(span.termination_date, datetime.date(2024, 4, 14))
        self.assertEqual(Member.objects.get(ssn="222334444").coverage_status, "TERMINATED")

    def test_dependent_without_subscriber_is_kept(self):
        """It used to violate a check constraint and disappear silently."""
        content = build_834(
            datetime.date(2024, 4, 1),
            [(False, "19", "021", "DOE", "KID", "333445555", "20150101", "M",
              "PPO-GOLD", "20240101", None)],
        )
        summary = sync_uploaded_file(
            self._upload(content, "d.txt", datetime.date(2024, 4, 1)), self.user
        )
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(Member.objects.filter(ssn="333445555").exists())

    def test_dependent_is_linked_to_the_preceding_subscriber(self):
        content = build_834(
            datetime.date(2024, 4, 1),
            [
                (True, "18", "021", "DOE", "JANE", "111223333", "19800101", "F",
                 "PPO-GOLD", "20240101", None),
                (False, "19", "021", "DOE", "KID", "333445555", "20150101", "M",
                 "PPO-GOLD", "20240101", None),
            ],
        )
        sync_uploaded_file(self._upload(content, "e.txt", datetime.date(2024, 4, 1)), self.user)
        kid = Member.objects.get(ssn="333445555")
        self.assertEqual(kid.member_type, "DEP")
        self.assertEqual(kid.subscriber.ssn, "111223333")


@override_settings(MEDIA_ROOT=MEDIA)
class InfoSectionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "boss", password="pw12345678", is_staff=True
        )
        get_user_model().objects.create_user("plain", password="pw12345678")

        full = build_834(
            datetime.date(2024, 4, 1),
            [
                (True, "18", "030", "STAY", "SAM", "111000111", "19800101", "M",
                 "PPO-GOLD", "20240101", None),
                (True, "18", "030", "LEAVE", "LEE", "222000222", "19850101", "F",
                 "PPO-GOLD", "20240101", None),
            ],
        )
        change = build_834(
            datetime.date(2024, 4, 15),
            [(True, "18", "024", "LEAVE", "LEE", "222000222", "19850101", "F",
              "PPO-GOLD", "20240101", "20240414")],
            control=2,
        )
        for payload, name, when in (
            (full, "full.txt", datetime.date(2024, 4, 1)),
            (change, "change.txt", datetime.date(2024, 4, 15)),
        ):
            record = UploadedFile(
                owner=self.admin,
                original_filename=name,
                file_size_bytes=len(payload),
                content_sha256=name,
                file_date=when,
            )
            record.stored_file.save(name, SimpleUploadedFile(name, payload), save=False)
            record.save()
            sync_uploaded_file(record, self.admin)

        self.http = Client()
        self.http.login(username="boss", password="pw12345678")

    def test_non_admin_is_refused(self):
        other = Client()
        other.login(username="plain", password="pw12345678")
        for path in (
            "/api/members/roster/",
            "/api/members/ssn-options/",
            "/api/members/file-dates/",
        ):
            self.assertEqual(other.get(path).status_code, 403, path)

    def test_anonymous_is_refused(self):
        self.assertEqual(Client().get("/api/members/roster/").status_code, 403)

    def test_file_dates_newest_first(self):
        dates = [d["file_date"] for d in self.http.get("/api/members/file-dates/").json()]
        self.assertEqual(dates, ["2024-04-15", "2024-04-01"])

    def test_presence_and_file_appearance_are_separate(self):
        """
        On 2024-04-15 the leaver is in the file and not covered, while the
        stayer is covered and not in the file. Both readings are correct, and
        collapsing them into one boolean gets both of them wrong.
        """
        body = self.http.get(
            "/api/members/roster/", {"file_date": "2024-04-15", "page_size": 50}
        ).json()
        rows = {r["last_name"]: r for r in body["results"]}

        self.assertEqual(rows["LEAVE"]["presence"], "ABSENT")
        self.assertTrue(rows["LEAVE"]["in_file"])
        self.assertEqual(rows["LEAVE"]["termination_date"], "2024-04-14")

        self.assertEqual(rows["STAY"]["presence"], "PRESENT")
        self.assertFalse(rows["STAY"]["in_file"])

        self.assertEqual(
            body["counts"], {"total": 2, "present": 1, "absent": 1, "in_file": 1}
        )

    def test_terminated_member_was_present_before_the_termination(self):
        body = self.http.get(
            "/api/members/roster/", {"file_date": "2024-04-01", "page_size": 50}
        ).json()
        rows = {r["last_name"]: r for r in body["results"]}
        self.assertEqual(rows["LEAVE"]["presence"], "PRESENT")
        self.assertEqual(body["counts"]["present"], 2)

    def test_defaults_to_the_newest_file_date(self):
        body = self.http.get("/api/members/roster/").json()
        self.assertEqual(body["file_date"], "2024-04-15")

    def test_ssn_filter(self):
        body = self.http.get("/api/members/roster/", {"ssn": "222000222"}).json()
        self.assertEqual([r["last_name"] for r in body["results"]], ["LEAVE"])

    def test_ssn_filter_accepts_dashes(self):
        body = self.http.get("/api/members/roster/", {"ssn": "222-00-0222"}).json()
        self.assertEqual([r["last_name"] for r in body["results"]], ["LEAVE"])

    def test_name_search(self):
        body = self.http.get("/api/members/roster/", {"q": "sam"}).json()
        self.assertEqual([r["last_name"] for r in body["results"]], ["STAY"])

    def test_name_search_across_both_name_columns(self):
        body = self.http.get("/api/members/roster/", {"q": "lee leave"}).json()
        self.assertEqual([r["last_name"] for r in body["results"]], ["LEAVE"])

    def test_presence_filter(self):
        body = self.http.get(
            "/api/members/roster/", {"file_date": "2024-04-15", "presence": "ABSENT"}
        ).json()
        self.assertEqual([r["last_name"] for r in body["results"]], ["LEAVE"])

    def test_member_type_filter(self):
        body = self.http.get("/api/members/roster/", {"member_type": "DEP"}).json()
        self.assertEqual(body["count"], 0)

    def test_unknown_date_returns_an_explanation_not_a_crash(self):
        body = self.http.get("/api/members/roster/", {"file_date": "2099-01-01"}).json()
        self.assertEqual(body["results"], [])
        self.assertIn("No 834", body["detail"])

    def test_pagination(self):
        body = self.http.get("/api/members/roster/", {"page_size": 1}).json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["total_pages"], 2)

    def test_ssn_options_are_masked_in_the_label(self):
        options = self.http.get("/api/members/ssn-options/").json()
        self.assertEqual(len(options), 2)
        self.assertTrue(all(o["masked"].startswith("XXX-XX-") for o in options))
        self.assertTrue(all(o["masked"] in o["label"] for o in options))


class SessionTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "admin2", email="a@b.com", password="pw12345678", is_staff=True
        )

    def test_login_reports_the_role_from_the_server(self):
        body = Client().post(
            "/api/users/login/",
            {"username": "admin2", "password": "pw12345678"},
            content_type="application/json",
        ).json()
        self.assertTrue(body["is_admin"])
        self.assertEqual(body["role"], "Platform Admin")

    def test_login_by_email(self):
        response = Client().post(
            "/api/users/login/",
            {"username": "a@b.com", "password": "pw12345678"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def test_bad_password_is_rejected(self):
        response = Client().post(
            "/api/users/login/",
            {"username": "admin2", "password": "nope"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_csrf_endpoint_sets_the_cookie(self):
        http = Client(enforce_csrf_checks=True)
        response = http.get("/api/users/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)

    def test_me_and_logout(self):
        http = Client()
        self.assertFalse(http.get("/api/users/me/").json()["authenticated"])
        http.post(
            "/api/users/login/",
            {"username": "admin2", "password": "pw12345678"},
            content_type="application/json",
        )
        self.assertTrue(http.get("/api/users/me/").json()["authenticated"])
        self.assertEqual(http.post("/api/users/logout/").status_code, 200)
        self.assertFalse(http.get("/api/users/me/").json()["authenticated"])

    def test_logout_when_already_logged_out_is_not_an_error(self):
        self.assertEqual(Client().post("/api/users/logout/").status_code, 200)

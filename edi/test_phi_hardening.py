"""
Tests for the PHI-handling hardening pass.

Three things are locked down here: that a full SSN never leaves the API, that
the application keeps working once the plaintext column is purged, and that the
SSN pepper can be moved independently of SECRET_KEY.
"""

import json
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from members.models import Member, ssn_fingerprint, ssn_last4_of
from users.models import Client, ClientMembership

from .test_regressions import SUBSCRIBER, VALID_834, build_834


class SsnExposureTests(TestCase):
    """A full SSN must not appear in any API response."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("owner", password="pw")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_login(self.user)

    def upload(self, content=VALID_834, name="t.x12"):
        """Upload then validate, matching the portal's two-step flow."""
        created = self.client.post(
            "/api/edi/upload/",
            {"file": SimpleUploadedFile(name, content.encode())},
        )
        body = created.json()
        if created.status_code == 201 and body.get("uploaded_file_id"):
            validated = self.client.post(
                "/api/edi/validate/",
                json.dumps({"uploaded_file_id": body["uploaded_file_id"]}),
                content_type="application/json",
            )
            # Callers unpack validation results off the upload response, so
            # merge the stored outcome onto it the way the screen reads it
            # back from /uploads/ after a refresh.
            merged = dict(body)
            vbody = validated.json()
            merged["status"] = vbody.get("status", merged.get("status"))
            merged["is_valid"] = vbody.get("is_valid")
            merged["errors"] = vbody.get("errors", [])
            merged["warnings"] = vbody.get("warnings", [])
            merged["member_loop_count"] = vbody.get("member_loop_count")
            merged["members_synced"] = vbody.get("members_synced", 0)
            created.json = lambda m=merged: m
        return created

    def test_member_search_returns_a_mask_not_the_digits(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                response = self.client.get("/api/members/search/", {"q": "111223333"})
                self.assertEqual(response.status_code, 200)

                body = response.json()[0]
                self.assertEqual(body["masked_ssn"], "XXX-XX-3333")
                self.assertEqual(body["ssn_last4"], "3333")
                self.assertNotIn("ssn", body)
                self.assertNotIn("111223333", response.content.decode())

    def test_roster_rows_carry_no_plaintext(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                response = self.client.get("/api/members/roster/")
                self.assertEqual(response.status_code, 200, response.content)
                self.assertNotIn("111223333", response.content.decode())
                row = response.json()["results"][0]
                self.assertNotIn("ssn", row)
                self.assertTrue(row["masked_ssn"].startswith("XXX-XX-"))

    def test_ssn_dropdown_offers_an_opaque_value(self):
        """
        The dropdown used to hand the browser every member's nine digits as the
        option value, purely so the roster filter could echo it back.
        """
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                response = self.client.get("/api/members/ssn-options/")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("111223333", response.content.decode())

                option = response.json()[0]
                self.assertEqual(option["value"], ssn_fingerprint("111223333"))
                self.assertTrue(option["masked"].startswith("XXX-XX-"))

    def test_the_dropdown_value_still_filters_the_roster(self):
        """Opaque is only acceptable if it remains usable."""
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                token = self.client.get("/api/members/ssn-options/").json()[0]["value"]

                filtered = self.client.get("/api/members/roster/", {"ssn": token})
                self.assertEqual(filtered.status_code, 200)
                rows = filtered.json()["results"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["last_name"], "WHITFIELD")


class PlaintextPurgeTests(TestCase):
    """The application must survive losing the plaintext column."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("owner", password="pw")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_login(self.user)

    def upload(self, content=VALID_834, name="t.x12"):
        """Upload then validate, matching the portal's two-step flow."""
        created = self.client.post(
            "/api/edi/upload/",
            {"file": SimpleUploadedFile(name, content.encode())},
        )
        body = created.json()
        if created.status_code == 201 and body.get("uploaded_file_id"):
            validated = self.client.post(
                "/api/edi/validate/",
                json.dumps({"uploaded_file_id": body["uploaded_file_id"]}),
                content_type="application/json",
            )
            # Callers unpack validation results off the upload response, so
            # merge the stored outcome onto it the way the screen reads it
            # back from /uploads/ after a refresh.
            merged = dict(body)
            vbody = validated.json()
            merged["status"] = vbody.get("status", merged.get("status"))
            merged["is_valid"] = vbody.get("is_valid")
            merged["errors"] = vbody.get("errors", [])
            merged["warnings"] = vbody.get("warnings", [])
            merged["member_loop_count"] = vbody.get("member_loop_count")
            merged["members_synced"] = vbody.get("members_synced", 0)
            created.json = lambda m=merged: m
        return created

    def test_last4_is_derived_on_save(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                member = Member.objects.get(ssn_fingerprint=ssn_fingerprint("111223333"))
                self.assertEqual(member.ssn_last4, "3333")
                self.assertEqual(member.masked_ssn, "XXX-XX-3333")

    def test_purge_refuses_without_confirmation(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                with self.assertRaises(CommandError):
                    call_command("purge_plaintext_ssn")
                self.assertTrue(Member.objects.exclude(ssn="").exists())

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                call_command("purge_plaintext_ssn", dry_run=True)
                self.assertTrue(Member.objects.exclude(ssn="").exists())

    def test_purge_refuses_when_derived_columns_are_missing(self):
        """Never destroy the source before the substitutes exist."""
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                Member.objects.update(ssn_last4="")
                with self.assertRaises(CommandError):
                    call_command("purge_plaintext_ssn", confirm=True)
                self.assertTrue(Member.objects.exclude(ssn="").exists())

    def test_search_and_masking_survive_the_purge(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload()
                call_command("purge_plaintext_ssn", confirm=True)

                self.assertFalse(Member.objects.exclude(ssn="").exists())
                member = Member.objects.get(ssn_fingerprint=ssn_fingerprint("111223333"))
                self.assertNotEqual(member.ssn_fingerprint, "")
                self.assertEqual(member.masked_ssn, "XXX-XX-3333")

                # Lookup by the full SSN still resolves, because it matches on
                # the fingerprint rather than the column that has just gone.
                response = self.client.get("/api/members/search/", {"q": "111223333"})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()[0]["masked_ssn"], "XXX-XX-3333")

    def test_identity_matching_survives_the_purge(self):
        """
        A second file for the same person must update the existing member
        rather than create a duplicate once the plaintext is gone.
        """
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.upload(build_834([SUBSCRIBER], control="0001"), "first.x12")
                call_command("purge_plaintext_ssn", confirm=True)
                self.assertEqual(Member.objects.count(), 1)

                self.upload(build_834([SUBSCRIBER], control="0002"), "second.x12")
                self.assertEqual(Member.objects.count(), 1)


class SsnPepperTests(TestCase):
    """The pepper is its own secret, not SECRET_KEY."""

    def test_pepper_defaults_to_secret_key_for_existing_data(self):
        """Backwards compatibility: an existing database keeps resolving."""
        with override_settings(SSN_PEPPER=""):
            from django.conf import settings

            expected = ssn_fingerprint("111223333")
            self.assertNotEqual(expected, "")
            with override_settings(SSN_PEPPER=settings.SECRET_KEY):
                self.assertEqual(ssn_fingerprint("111223333"), expected)

    def test_a_different_pepper_produces_a_different_fingerprint(self):
        with override_settings(SSN_PEPPER="pepper-one"):
            first = ssn_fingerprint("111223333")
        with override_settings(SSN_PEPPER="pepper-two"):
            second = ssn_fingerprint("111223333")
        self.assertNotEqual(first, second)

    def test_rotating_secret_key_no_longer_moves_fingerprints(self):
        """
        This is the failure the split prevents: rotating SECRET_KEY used to
        re-key every stored fingerprint, so member matching would silently stop
        matching and start creating duplicates.
        """
        with override_settings(SSN_PEPPER="a-dedicated-pepper"):
            with override_settings(SECRET_KEY="original-secret"):
                before = ssn_fingerprint("111223333")
            with override_settings(SECRET_KEY="rotated-secret"):
                after = ssn_fingerprint("111223333")
        self.assertEqual(before, after)

    def test_punctuation_still_normalises(self):
        self.assertEqual(ssn_fingerprint("111-22-3333"), ssn_fingerprint("111223333"))
        self.assertEqual(ssn_last4_of("111-22-3333"), "3333")


class RosterTenancyTests(TestCase):
    """Staff roster endpoints respect the selected client."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user("staff", password="pw")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])

        self.plan_a = Client.objects.create(code="plan-a", name="Plan A")
        self.plan_b = Client.objects.create(code="plan-b", name="Plan B")
        ClientMembership.objects.create(
            user=self.staff, client=self.plan_a, is_default=True
        )
        ClientMembership.objects.create(user=self.staff, client=self.plan_b)
        self.client.force_login(self.staff)

    def test_roster_is_scoped_to_the_selected_client(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                # Uploaded while acting for Plan A.
                created = self.client.post(
                    "/api/edi/upload/",
                    {"file": SimpleUploadedFile("a.x12", VALID_834.encode())},
                    HTTP_X_CLIENT_ID=str(self.plan_a.id),
                ).json()
                # Validation (and the member sync it triggers) runs under the
                # same client, as the screen does when Validate is pressed.
                self.client.post(
                    "/api/edi/validate/",
                    json.dumps({"uploaded_file_id": created["uploaded_file_id"]}),
                    content_type="application/json",
                    HTTP_X_CLIENT_ID=str(self.plan_a.id),
                )

                on_a = self.client.get(
                    "/api/members/roster/", HTTP_X_CLIENT_ID=str(self.plan_a.id)
                )
                self.assertEqual(on_a.status_code, 200, on_a.content)
                self.assertGreater(len(on_a.json()["results"]), 0)

                # Plan B holds no uploads, so it sees no roster at all — the
                # endpoint reports that plainly rather than falling through to
                # Plan A's members.
                on_b = self.client.get(
                    "/api/members/roster/", HTTP_X_CLIENT_ID=str(self.plan_b.id)
                )
                self.assertEqual(on_b.status_code, 200, on_b.content)
                self.assertEqual(on_b.json()["results"], [])
                self.assertIsNone(on_b.json()["file"])

    def test_a_client_the_user_does_not_hold_is_refused(self):
        outsider = Client.objects.create(code="plan-c", name="Plan C")
        response = self.client.get(
            "/api/members/roster/", HTTP_X_CLIENT_ID=str(outsider.id)
        )
        self.assertEqual(response.status_code, 403, response.content)

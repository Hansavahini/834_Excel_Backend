"""
Regression tests for the defects found in the conversion workflow.

Each test names the bug it locks down. They run against a small synthetic
interchange rather than a client file, because a test fixture built from real
834 data is PHI and must not live in the repository.
"""

import os
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from edi.services.file_service import UnsafePathError, get_file_path
from edi.services.loop_extractor import StreamingParsedFile, extract_loops
from edi.services.parser import Delimiters, EDI834Parser, EDIParseError, envelope_facts
from edi.services.row_builder import build_excel_rows
from edi.services.transforms import apply_transform
from edi.services.validator import validate_834

ISA = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    "*250701*0839*^*00501*000000001*0*P*:~"
)
GS = "GS*BE*SENDER*RECEIVER*20250701*0839*1*X*005010X220A1~"
HEADER = (
    "ST*834*0001*005010X220A1~BGN*00*0001*20250702*0839****4~"
    "N1*P5*ACME TRUST*FI*123456789~N1*IN*BIG CARRIER*FI*987654321~"
)
SUBSCRIBER = (
    "INS*Y*18*030**A***FT~REF*0F*HW001~REF*1L*GRP9~"
    "NM1*IL*1*SMITH*JOHN*Q***34*111223333~"
    "N3*1 MAIN ST~N4*DAYTON*OH*45402~DMG*D8*19800115*M~"
    "HD*030**HLT~DTP*348*D8*20250101~DTP*349*D8*20251231~"
)
DEPENDENT = (
    "INS*N*01*030**A~REF*0F*HW001~"
    "NM1*IL*1*SMITH*MARY****34*444556666~"
    "NM1*S3*1*SMITH*ROBERT~"
    "DMG*D8*19820320*F~HD*030**DEN~DTP*348*D8*20250201~"
)
TRAILER = "SE*22*0001~GE*1*1~IEA*1*000000001~"

SAMPLE = ISA + GS + HEADER + SUBSCRIBER + DEPENDENT + TRAILER


def write_temp(content, suffix=".x12"):
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    handle.write(content)
    handle.close()
    return handle.name


class ParserTests(TestCase):
    def setUp(self):
        self.path = write_temp(SAMPLE)

    def tearDown(self):
        os.unlink(self.path)

    def test_segments_keep_their_elements_together(self):
        """The old parser flattened to elements and lost which NM1 a value came from."""
        segments = EDI834Parser(self.path).parse()
        nm1s = [s for s in segments if s.name == "NM1"]
        self.assertEqual(len(nm1s), 3)
        self.assertEqual(nm1s[0].get(1), "IL")
        self.assertEqual(nm1s[0].get(3), "SMITH")
        self.assertEqual(nm1s[2].get(1), "S3")  # custodial parent, distinguishable

    def test_delimiters_are_read_from_isa_not_assumed(self):
        odd = SAMPLE.replace("*", "|").replace("~", "\n")
        path = write_temp(odd)
        try:
            segments = EDI834Parser(path).parse()
            self.assertTrue(any(s.name == "INS" for s in segments))
            self.assertEqual(len([s for s in segments if s.name == "NM1"]), 3)
        finally:
            os.unlink(path)

    def test_non_x12_input_is_rejected(self):
        path = write_temp("%PDF-1.7 this is not an interchange")
        try:
            with self.assertRaises(EDIParseError):
                EDI834Parser(path).parse()
        finally:
            os.unlink(path)

    def test_envelope_facts_prefer_bgn03_over_gs04(self):
        facts = envelope_facts(EDI834Parser(self.path).parse())
        self.assertEqual(facts["file_date"], "20250702")
        self.assertEqual(facts["sponsor_name"], "ACME TRUST")
        self.assertEqual(facts["interchange_control_number"], "000000001")


class ValidatorTests(TestCase):
    def test_clean_file_passes(self):
        path = write_temp(SAMPLE)
        try:
            result = validate_834(EDI834Parser(path).iter_segments())
            self.assertTrue(result.is_valid, result.errors)
            self.assertEqual(result.member_loop_count, 2)
            self.assertTrue(result.is_full_file)
        finally:
            os.unlink(path)

    def test_truncated_file_is_caught(self):
        """Previously validator.py was empty and a truncated file parsed silently."""
        path = write_temp(ISA + GS + HEADER + SUBSCRIBER)
        try:
            result = validate_834(EDI834Parser(path).iter_segments())
            self.assertFalse(result.is_valid)
            self.assertTrue(any("truncated" in e for e in result.errors))
        finally:
            os.unlink(path)

    def test_wrong_transaction_set_is_rejected(self):
        path = write_temp(SAMPLE.replace("ST*834*", "ST*270*"))
        try:
            result = validate_834(EDI834Parser(path).iter_segments())
            self.assertFalse(result.is_valid)
            self.assertTrue(any("not an 834" in e for e in result.errors))
        finally:
            os.unlink(path)

    def test_control_number_mismatch_is_reported(self):
        path = write_temp(SAMPLE.replace("IEA*1*000000001~", "IEA*1*000000009~"))
        try:
            result = validate_834(EDI834Parser(path).iter_segments())
            self.assertTrue(any("control numbers disagree" in e for e in result.errors))
        finally:
            os.unlink(path)


class LoopExtractionTests(TestCase):
    def setUp(self):
        self.path = write_temp(SAMPLE)
        self.segments = EDI834Parser(self.path).parse()

    def tearDown(self):
        os.unlink(self.path)

    def test_one_loop_per_ins_segment(self):
        """The old extractor flushed per INS *element*, inflating 2 loops into 9."""
        parsed = extract_loops(self.segments)
        self.assertEqual(len(parsed.loops), 2)

    def test_header_is_not_emitted_as_a_member(self):
        """Loop 1 used to be ISA/GS/ST/BGN, producing a phantom first row."""
        parsed = extract_loops(self.segments)
        self.assertEqual(parsed.loops[0].ins.get(1), "Y")
        self.assertIn("ISA", [s.name for s in parsed.header])
        self.assertNotIn("ISA", [s.name for s in parsed.loops[0].segments])

    def test_trailers_do_not_land_in_the_last_member(self):
        parsed = extract_loops(self.segments)
        self.assertNotIn("SE", [s.name for s in parsed.loops[-1].segments])

    def test_streaming_and_materialised_agree(self):
        streamed = StreamingParsedFile(iter(self.segments))
        loops = list(streamed)
        self.assertEqual(len(loops), 2)
        self.assertEqual(streamed.subscriber_count, 1)
        self.assertEqual(streamed.dependent_count, 1)

    def test_legacy_flat_dicts_raise_instead_of_silently_misbehaving(self):
        flat = EDI834Parser(self.path).extract_elements()
        with self.assertRaises(TypeError):
            extract_loops(flat)


class RowBuilderTests(TestCase):
    def setUp(self):
        self.path = write_temp(SAMPLE)
        self.parsed = extract_loops(EDI834Parser(self.path).parse())

    def tearDown(self):
        os.unlink(self.path)

    def test_qualifier_disambiguates_repeated_segments(self):
        """DTP03 is a begin date under 348 and a term date under 349."""
        rules = [
            {"excel_column": "EFF", "segment": "DTP", "element": "DTP03",
             "qualifier_element": "DTP01", "qualifier_value": "348"},
            {"excel_column": "TERM", "segment": "DTP", "element": "DTP03",
             "qualifier_element": "DTP01", "qualifier_value": "349"},
        ]
        rows = build_excel_rows(self.parsed.loops, rules)
        self.assertEqual(rows[0]["EFF"], "20250101")
        self.assertEqual(rows[0]["TERM"], "20251231")

    def test_custodial_parent_does_not_overwrite_the_insured(self):
        rules = [{"excel_column": "LAST", "segment": "NM1", "element": "NM103",
                  "qualifier_element": "NM101", "qualifier_value": "IL"}]
        rows = build_excel_rows(self.parsed.loops, rules)
        self.assertEqual(rows[1]["LAST"], "SMITH")
        self.assertEqual(len(rows), 2)

    def test_ref_qualifier_separates_ssn_from_group_number(self):
        rules = [
            {"excel_column": "SUBNO", "segment": "REF", "element": "REF02",
             "qualifier_element": "REF01", "qualifier_value": "0F"},
            {"excel_column": "GROUP", "segment": "REF", "element": "REF02",
             "qualifier_element": "REF01", "qualifier_value": "1L"},
        ]
        rows = build_excel_rows(self.parsed.loops, rules)
        self.assertEqual(rows[0]["SUBNO"], "HW001")
        self.assertEqual(rows[0]["GROUP"], "GRP9")

    def test_dependent_rows_inherit_subscriber_scoped_columns(self):
        """
        A SUB-scoped rule fills from the family's subscriber on every row.

        The old behaviour blanked the column on dependent rows, which left a
        flat sheet where a dependent could not be tied to its subscriber
        without counting rows. The roster layout identifies the family by
        repeating the subscriber's values down the front columns instead.
        """
        rules = [{"excel_column": "SUB NAME", "segment": "NM1", "element": "NM103",
                  "qualifier_element": "NM101", "qualifier_value": "IL", "applies_to": "SUB"}]
        rows = build_excel_rows(self.parsed.loops, rules)
        self.assertEqual(rows[0]["SUB NAME"], "SMITH")
        self.assertEqual(rows[1]["SUB NAME"], "SMITH")

    def test_row_count_matches_member_loops(self):
        """The header loop used to add one phantom row to every workbook."""
        rules = [{"excel_column": "X", "segment": "INS", "element": "INS01"}]
        rows = build_excel_rows(self.parsed.loops, rules)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["X"] for r in rows], ["Y", "N"])


class TransformTests(TestCase):
    def test_transforms_are_implemented(self):
        """MappingDetail has offered these since day one; none of them did anything."""
        # Part 4: the portal's one date format is MM-DD-YYYY. The slashed form
        # is still reachable by name so a saved template that asked for it does
        # not silently change meaning.
        self.assertEqual(apply_transform("20250101", "DATE_MDY"), "01-01-2025")
        self.assertEqual(apply_transform("20250101", "DATE_MDY_SLASH"), "01/01/2025")
        self.assertEqual(apply_transform("20250101", "DATE_ISO"), "2025-01-01")
        self.assertEqual(apply_transform("111223333", "SSN_DASHED"), "111-22-3333")
        # Part 14: nine digits, leading zero intact, and nothing else accepted.
        self.assertEqual(apply_transform("001234567", "SSN"), "001234567")
        self.assertEqual(apply_transform("12345678", "SSN"), "")
        self.assertEqual(apply_transform("111223333", "SSN_LAST4"), "XXX-XX-3333")
        self.assertEqual(apply_transform("9375551234", "PHONE"), "(937) 555-1234")

    def test_malformed_values_pass_through_rather_than_raising(self):
        self.assertEqual(apply_transform("not a date", "DATE_MDY"), "not a date")
        self.assertEqual(apply_transform("", "PHONE"), "")


class PathSafetyTests(TestCase):
    def test_traversal_is_refused(self):
        for attempt in ("../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd"):
            with self.assertRaises(UnsafePathError):
                get_file_path(attempt)

    def test_empty_path_is_refused(self):
        with self.assertRaises(UnsafePathError):
            get_file_path("")


class EndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="pw")
        self.client.force_login(self.user)

    def test_endpoints_require_authentication(self):
        """Every endpoint was open; these records carry PHI."""
        self.client.logout()
        for url in ("/api/edi/upload/", "/api/edi/convert/", "/api/edi/mappings/"):
            self.assertIn(self.client.post(url, {}).status_code, (401, 403), url)

    def test_convert_with_missing_fields_is_a_400_not_a_500(self):
        """request.data["file_path"] used to raise KeyError straight to a 500."""
        response = self.client.post("/api/edi/convert/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_a_renamed_non_edi_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bogus = SimpleUploadedFile("payroll.x12", b"%PDF-1.7 nope", content_type="text/plain")
        response = self.client.post("/api/edi/upload/", {"file": bogus})
        self.assertEqual(response.status_code, 400)

    def test_full_pipeline_writes_history_and_a_downloadable_workbook(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                upload = SimpleUploadedFile("t.x12", SAMPLE.encode(), content_type="text/plain")
                created = self.client.post("/api/edi/upload/", {"file": upload})
                self.assertEqual(created.status_code, 201, created.content)

                # Upload stores; Validate parses, counts and syncs.
                validated = self.client.post(
                    "/api/edi/validate/",
                    {"uploaded_file_id": created.json()["uploaded_file_id"]},
                    content_type="application/json",
                )
                self.assertEqual(validated.json()["member_loop_count"], 2)

                converted = self.client.post(
                    "/api/edi/convert/",
                    {
                        "uploaded_file_id": created.json()["uploaded_file_id"],
                        "mappings": [
                            {"excel_column": "LAST", "segment": "NM1", "element": "NM103",
                             "qualifier_element": "NM101", "qualifier_value": "IL"}
                        ],
                    },
                    content_type="application/json",
                )
                self.assertEqual(converted.status_code, 200, converted.content)
                body = converted.json()
                self.assertEqual(body["rows_generated"], 2)
                self.assertEqual(body["subscribers"], 1)
                self.assertEqual(body["dependents"], 1)

                # The workbook must be retrievable; there was no download route at all.
                self.assertEqual(self.client.get(body["download_url"]).status_code, 200)

                # And the run must be on the record.
                history = self.client.get("/api/edi/history/").json()
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["status"], "SUCCESS")
                self.assertEqual(history[0]["rows_written"], 2)

    def test_another_user_cannot_download_my_workbook(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                upload = SimpleUploadedFile("t.x12", SAMPLE.encode(), content_type="text/plain")
                created = self.client.post("/api/edi/upload/", {"file": upload})
                self.client.post(
                    "/api/edi/validate/",
                    {"uploaded_file_id": created.json()["uploaded_file_id"]},
                    content_type="application/json",
                )
                converted = self.client.post(
                    "/api/edi/convert/",
                    {"uploaded_file_id": created.json()["uploaded_file_id"],
                     "mappings": [{"excel_column": "LAST", "segment": "NM1", "element": "NM103"}]},
                    content_type="application/json",
                )
                url = converted.json()["download_url"]

                get_user_model().objects.create_user("intruder", password="pw")
                self.client.logout()
                self.client.login(username="intruder", password="pw")
                self.assertEqual(self.client.get(url).status_code, 404)

"""
Sample data for development. Builds two consecutive daily files with a real
change between them so the comparison and daily status screens have something
to render: one family added, one dependent terminated, one plan change, and one
member who reinstates after a gap.

All names and identifiers here are invented. Never seed a shared development
database from a client 834, even a small one.
"""

import datetime as dt
import hashlib

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from conversion.models import ConversionHistory, FileComparison
from files.models import ProcessingStatus, UploadedFile
from mapping.models import MappingDetail, MappingTemplate, SegmentElement
from members.models import (
    CoverageStatus,
    CustodialParent,
    Member,
    MemberDailyStatus,
    MemberEligibilityHistory,
    MemberType,
    RelationshipCode,
)

EXCEL_COLUMNS = [
    # (order, excel column, segment, element, qualifier element, qualifier value, applies_to, transform)
    (1, "SUB/DEP", "INS", "INS01", "", "", "BOTH", "NONE"),
    (2, "LAST NAME", "NM1", "NM103", "NM101", "IL", "SUB", "UPPER"),
    (3, "FIRST NAME", "NM1", "NM104", "NM101", "IL", "SUB", "UPPER"),
    (4, "SSN", "REF", "REF02", "REF01", "0F", "SUB", "SSN_DASHED"),
    (5, "SEX", "DMG", "DMG03", "", "", "SUB", "NONE"),
    (6, "DOB", "DMG", "DMG02", "", "", "SUB", "DATE_MDY"),
    (7, "DEP LAST NAME", "NM1", "NM103", "NM101", "IL", "DEP", "UPPER"),
    (8, "DEP FIRST NAME", "NM1", "NM104", "NM101", "IL", "DEP", "UPPER"),
    (9, "PLAN", "HD", "HD04", "", "", "BOTH", "NONE"),
    (10, "EFF DATE", "DTP", "DTP03", "DTP01", "348", "BOTH", "DATE_MDY"),
    (11, "TERM DATE", "DTP", "DTP03", "DTP01", "349", "BOTH", "DATE_MDY"),
    (13, "DEP SSN", "REF", "REF02", "REF01", "0F", "DEP", "SSN_DASHED"),
    (14, "DEP SEX", "DMG", "DMG03", "", "", "DEP", "NONE"),
    (15, "DEP DOB", "DMG", "DMG02", "", "", "DEP", "DATE_MDY"),
    (16, "LOCAL", "REF", "REF02", "REF01", "LU", "SUB", "NONE"),
    (17, "CUSTODIAL PARENT", "NM1", "NM103", "NM101", "IL", "DEP", "UPPER"),
    (18, "CLASS", "HD", "HD05", "", "", "BOTH", "NONE"),
    (19, "ID", "NM1", "NM109", "NM101", "IL", "BOTH", "NONE"),
    (20, "ADDRESS1", "N3", "N301", "", "", "BOTH", "UPPER"),
    (21, "ADDRESS2", "N3", "N302", "", "", "BOTH", "UPPER"),
    (22, "CITY", "N4", "N401", "", "", "BOTH", "UPPER"),
    (23, "STATE", "N4", "N402", "", "", "BOTH", "UPPER"),
    (24, "ZIP", "N4", "N403", "", "", "BOTH", "NONE"),
    (25, "PHONE", "PER", "PER04", "PER03", "TE", "BOTH", "PHONE_DASHED"),
    (26, "EMAIL", "PER", "PER04", "PER03", "EM", "BOTH", "LOWER"),
    (27, "STATUS", "INS", "INS05", "", "", "BOTH", "NONE"),
    (28, "TYPE", "INS", "INS03", "", "", "BOTH", "NONE"),
    (29, "MEMBER ID", "NM1", "NM109", "NM101", "IL", "BOTH", "NONE"),
    (30, "CUSTODIAL ADDRESS1", "N3", "N301", "", "", "DEP", "UPPER"),
    (31, "CUSTODIAL ADDRESS2", "N3", "N302", "", "", "DEP", "UPPER"),
    (32, "CUSTODIAL CITY", "N4", "N401", "", "", "DEP", "UPPER"),
    (33, "CUSTODIAL STATE", "N4", "N402", "", "", "DEP", "UPPER"),
    (34, "CUSTODIAL ZIP", "N4", "N403", "", "", "DEP", "NONE"),
    (35, "CUSTODIAL PHONE", "PER", "PER04", "PER03", "TE", "DEP", "PHONE_DASHED")
]


def fake_upload(owner, file_date, name, member_count):
    payload = "ISA*00*{name}~".format(name=name).encode()
    upload = UploadedFile(
        owner=owner,
        original_filename=name,
        file_size_bytes=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        interchange_control_number="000000123",
        sender_id="SPONSOR01",
        receiver_id="CARRIER01",
        sponsor_name="Acme Benefit Fund",
        file_date=file_date,
        is_full_file=True,
        processing_status=ProcessingStatus.PARSED,
        segment_count=member_count * 9,
        member_loop_count=member_count,
    )
    upload.stored_file.save(name, ContentFile(payload), save=False)
    upload.save()
    return upload


class Command(BaseCommand):
    help = "Load a small, self-consistent development dataset."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        owner, _ = User.objects.get_or_create(
            username="tpa_analyst", defaults={"email": "analyst@example.test", "is_staff": True}
        )

        template, _ = MappingTemplate.objects.get_or_create(
            owner=owner, mapping_name="Acme Benefit Fund Standard", version=1,
            defaults={"is_default": True, "description": "Baseline column layout for the Acme eligibility workbook."},
        )
        for order, column, seg, elem, q_elem, q_val, applies, transform in EXCEL_COLUMNS:
            element = SegmentElement.objects.filter(segment_name=seg, element_code=elem).first()
            if element is None:
                self.stdout.write(self.style.WARNING(
                    "Run seed_segment_elements first, {e} is missing.".format(e=elem)
                ))
                return
            MappingDetail.objects.get_or_create(
                mapping_template=template, excel_column=column,
                defaults={
                    "column_order": order, "segment_element": element,
                    "qualifier_element": q_elem, "qualifier_value": q_val,
                    "applies_to": applies, "transform": transform,
                },
            )

        day_one = dt.date(2026, 8, 3)
        day_two = dt.date(2026, 8, 4)
        file_one = fake_upload(owner, day_one, "acme_20260803.x12", 5)
        file_two = fake_upload(owner, day_two, "acme_20260804.x12", 6)

        def add_member(**kwargs):
            kwargs.setdefault("owner", owner)
            kwargs.setdefault("first_seen_file", file_one)
            kwargs.setdefault("last_seen_file", file_two)
            return Member.objects.create(**kwargs)

        sub_a = add_member(
            member_type=MemberType.SUBSCRIBER, member_id="A100001", subscriber_number="A100001",
            group_number="GRP-4410", first_name="Marcus", last_name="Whitfield",
            ssn="400112233", gender_code="M", date_of_birth=dt.date(1979, 4, 12),
            local="Local 218", plan_code="PPO-STD", class_code="ACTIVE-FT",
            address1="1420 Fairground Rd", city="Springboro", state="OH", postal_code="450662",
            phone="9375550118", email="mwhitfield@example.test", coverage_status=CoverageStatus.ACTIVE,
            benefit_status_code="A",
        )
        dep_a1 = add_member(
            member_type=MemberType.DEPENDENT, subscriber=sub_a, relationship_code=RelationshipCode.SPOUSE,
            member_id="A100001-01", subscriber_number="A100001", first_name="Renata", last_name="Whitfield",
            ssn="400112244", gender_code="F", date_of_birth=dt.date(1981, 9, 30),
            plan_code="PPO-STD", class_code="ACTIVE-FT", coverage_status=CoverageStatus.ACTIVE,
        )
        dep_a2 = add_member(
            member_type=MemberType.DEPENDENT, subscriber=sub_a, relationship_code=RelationshipCode.CHILD,
            member_id="A100001-02", subscriber_number="A100001", first_name="Iris", last_name="Whitfield",
            gender_code="F", date_of_birth=dt.date(2006, 2, 14),
            plan_code="PPO-STD", coverage_status=CoverageStatus.TERMINATED, student_status_code="N",
        )
        CustodialParent.objects.get_or_create(
            member=dep_a2,
            defaults={
                "first_name": "Dana", "last_name": "Okonjo", "address1": "88 Ridgeway Ct",
                "city": "Dayton", "state": "OH", "postal_code": "45402", "phone": "9375550190",
            },
        )
        sub_b = add_member(
            member_type=MemberType.SUBSCRIBER, member_id="A100014", subscriber_number="A100014",
            group_number="GRP-4410", first_name="Priya", last_name="Raghunathan",
            ssn="400119876", gender_code="F", date_of_birth=dt.date(1990, 11, 2),
            local="Local 218", plan_code="HDHP", class_code="ACTIVE-FT",
            address1="9 Bellbrook Ave", city="Xenia", state="OH", postal_code="45385",
            coverage_status=CoverageStatus.ACTIVE, benefit_status_code="A",
        )
        sub_c = add_member(
            member_type=MemberType.SUBSCRIBER, member_id="A100027", subscriber_number="A100027",
            group_number="GRP-4410", first_name="Devon", last_name="Alcaraz",
            ssn="400117654", gender_code="M", date_of_birth=dt.date(1968, 7, 21),
            local="Local 306", plan_code="PPO-STD", class_code="RETIREE",
            city="Kettering", state="OH", postal_code="45429",
            coverage_status=CoverageStatus.COBRA, benefit_status_code="C",
            first_seen_file=file_two,
        )

        spans = [
            # Marcus and spouse, open since hire.
            (sub_a, "HLT", dt.date(2023, 1, 1), None, "021", file_one),
            (sub_a, "DEN", dt.date(2023, 1, 1), None, "021", file_one),
            (dep_a1, "HLT", dt.date(2023, 1, 1), None, "021", file_one),
            # Iris ages out, termination arrives in the second file.
            (dep_a2, "HLT", dt.date(2023, 1, 1), dt.date(2026, 8, 3), "024", file_two),
            # Priya has a gap and a reinstatement, two rows not two column pairs.
            (sub_b, "HLT", dt.date(2024, 3, 1), dt.date(2025, 6, 30), "024", file_one),
            (sub_b, "HLT", dt.date(2026, 1, 1), None, "025", file_one),
            # Devon appears for the first time in the second file.
            (sub_c, "HLT", dt.date(2026, 8, 4), None, "021", file_two),
        ]
        for member, line, eff, term, mtc, source in spans:
            MemberEligibilityHistory.objects.get_or_create(
                member=member, insurance_line_code=line, effective_date=eff,
                defaults={
                    "termination_date": term, "maintenance_type_code": mtc,
                    "plan_code": member.plan_code, "class_code": member.class_code, "source_file": source,
                },
            )

        presence = [
            (sub_a, file_one, day_one, "UNCHANGED", {}),
            (dep_a1, file_one, day_one, "UNCHANGED", {}),
            (dep_a2, file_one, day_one, "UNCHANGED", {}),
            (sub_b, file_one, day_one, "UNCHANGED", {}),
            (sub_a, file_two, day_two, "UNCHANGED", {}),
            (dep_a1, file_two, day_two, "CHANGED", {"plan_code": ["PPO-STD", "PPO-STD"]}),
            (dep_a2, file_two, day_two, "TERMINATED", {"coverage_status": ["ACTIVE", "TERMINATED"]}),
            (sub_b, file_two, day_two, "UNCHANGED", {}),
            (sub_c, file_two, day_two, "ADDED", {}),
        ]
        for member, source, date, change, diff in presence:
            MemberDailyStatus.objects.get_or_create(
                member=member, status_date=date,
                defaults={"uploaded_file": source, "change_type": change, "changed_fields": diff},
            )

        for source in (file_one, file_two):
            ConversionHistory.objects.get_or_create(
                uploaded_file=source,
                defaults={
                    "owner": owner, "mapping_template": template, "mapping_version": template.version,
                    "status": ConversionHistory.Status.QUEUED, "members_processed": source.member_loop_count,
                },
            )
        FileComparison.objects.get_or_create(
            baseline_file=file_one, current_file=file_two,
            defaults={
                "owner": owner, "added_count": 1, "terminated_count": 1,
                "changed_count": 1, "unchanged_count": 3, "dropped_count": 0,
            },
        )

        self.stdout.write(self.style.SUCCESS(
            "Seeded {m} members, {s} eligibility spans, 2 daily files.".format(
                m=Member.objects.count(), s=MemberEligibilityHistory.objects.count()
            )
        ))

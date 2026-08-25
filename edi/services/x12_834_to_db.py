"""
Turn a member loop into the field dict the Member model expects.

The previous version expected segments shaped as {"segment": ..., "elements": [...]},
which nothing in the codebase produced: the parser emitted flat element records,
so this module could not have been called successfully and in fact never was.
It also read NM1 unconditionally, meaning the sponsor and payer NM1 blocks would
overwrite the insured's name, and it indexed elements[2] for NM103 while the
parser had already dropped the segment id, an off-by-one waiting to happen.

Rewritten against Segment objects with the qualifiers applied.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

INSURED = "IL"
CUSTODIAL_PARENT = "S3"

# DTP01 date qualifiers that matter for a coverage span.
DTP_BENEFIT_BEGIN = "348"
DTP_BENEFIT_END = "349"

# REF01 qualifiers.
REF_SUBSCRIBER_NUMBER = "0F"
REF_GROUP_NUMBER = "1L"
REF_UNION_LOCAL = "LU"


def parse_x12_date(value: str) -> Optional[date]:
    """CCYYMMDD to a date, or None. Never raises on a malformed value."""
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def convert_834_to_member(loop) -> dict:
    """
    Extract member fields from one MemberLoop.

    Accepts a MemberLoop or a bare list of Segment objects.
    """
    segments = getattr(loop, "segments", loop)
    member: dict = {
        "member_type": "SUB",
        "relationship_code": "18",
        "first_name": "",
        "last_name": "",
        "middle_name": "",
        "name_suffix": "",
        "member_id": "",
        "ssn": "",
        "gender_code": "U",
        "date_of_birth": None,
        "subscriber_number": "",
        "group_number": "",
        "local": "",
        "address1": "",
        "address2": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "phone": "",
        "email": "",
        "benefit_status_code": "",
        "employment_status_code": "",
        "student_status_code": "",
        "plan_code": "",
        "insurance_line_code": "HLT",
        "effective_date": None,
        "termination_date": None,
        "maintenance_type_code": "",
        "maintenance_reason_code": "",
        # One entry per HD segment. A member with medical and dental on
        # different dates has two coverage lines, and collapsing them into a
        # single effective/termination pair loses one of them outright. The
        # flat keys above stay populated from the first line so existing
        # callers keep working.
        "coverages": [],
    }

    seen_insured_nm1 = False
    current_coverage = None

    for segment in segments:
        name = segment.name

        if name == "INS":
            member["member_type"] = "SUB" if segment.get(1).strip().upper() == "Y" else "DEP"
            member["relationship_code"] = segment.get(2).strip() or "18"
            member["maintenance_type_code"] = segment.get(3).strip()
            member["maintenance_reason_code"] = segment.get(4).strip()
            member["benefit_status_code"] = segment.get(5).strip()
            member["employment_status_code"] = segment.get(8).strip()
            member["student_status_code"] = segment.get(9).strip()

        elif name == "NM1":
            # Only the insured NM1. The custodial parent and any sponsor block
            # carry the same element positions and would otherwise overwrite it.
            if segment.get(1).strip().upper() != INSURED or seen_insured_nm1:
                continue
            seen_insured_nm1 = True
            member["last_name"] = segment.get(3).strip()[:60]
            member["first_name"] = segment.get(4).strip()[:35]
            member["middle_name"] = segment.get(5).strip()[:25]
            member["name_suffix"] = segment.get(7).strip()[:10]
            qualifier = segment.get(8).strip()
            if qualifier == "34":
                # NM108=34 means the identification code in NM109 is a Social
                # Security Number. Copying it into member_id — which is what
                # used to happen — put nine plaintext digits into a column that
                # the roster, the search results and the admin list all render
                # in clear, so masking the ssn column while this ran achieved
                # nothing at all. The SSN goes to the SSN field, where it is
                # fingerprinted and masked; member_id is left empty because the
                # sponsor did not supply a non-SSN identifier. Identity
                # resolution falls back to the fingerprint, so nothing is lost.
                digits = "".join(ch for ch in segment.get(9) if ch.isdigit())
                if len(digits) == 9:
                    member["ssn"] = digits
                else:
                    member["member_id"] = segment.get(9).strip()[:80]
            elif qualifier in ("ZZ", "MI"):
                member["member_id"] = segment.get(9).strip()[:80]

        elif name == "REF":
            qualifier = segment.get(1).strip().upper()
            value = segment.get(2).strip()
            if qualifier == REF_SUBSCRIBER_NUMBER:
                member["subscriber_number"] = value[:80]
            elif qualifier == REF_GROUP_NUMBER:
                member["group_number"] = value[:50]
            elif qualifier == REF_UNION_LOCAL:
                member["local"] = value[:30]

        elif name == "DMG":
            member["date_of_birth"] = parse_x12_date(segment.get(2))
            gender = segment.get(3).strip().upper()
            member["gender_code"] = gender if gender in ("M", "F") else "U"

        elif name == "N3":
            member["address1"] = segment.get(1).strip()[:55]
            member["address2"] = segment.get(2).strip()[:55]

        elif name == "N4":
            member["city"] = segment.get(1).strip()[:30]
            member["state"] = segment.get(2).strip().upper()[:2]
            member["postal_code"] = "".join(ch for ch in segment.get(3) if ch.isdigit())[:9]

        elif name == "PER":
            for number_index, qualifier_index in ((4, 3), (6, 5), (8, 7)):
                qualifier = segment.get(qualifier_index).strip().upper()
                value = segment.get(number_index).strip()
                if qualifier in ("TE", "HP", "WP") and not member["phone"]:
                    member["phone"] = "".join(ch for ch in value if ch.isdigit())[:10]
                elif qualifier == "EM" and not member["email"]:
                    member["email"] = value[:254]

        elif name == "HD":
            line_code = (segment.get(3).strip().upper() or "HLT")[:3]
            plan_code = (segment.get(4).strip() or segment.get(3).strip())[:30]
            current_coverage = {
                "insurance_line_code": line_code,
                "plan_code": plan_code,
                "maintenance_type_code": segment.get(1).strip()[:3],
                "effective_date": None,
                "termination_date": None,
            }
            member["coverages"].append(current_coverage)

        elif name == "DTP":
            qualifier = segment.get(1).strip()
            parsed = parse_x12_date(segment.get(3))
            if qualifier not in (DTP_BENEFIT_BEGIN, DTP_BENEFIT_END):
                continue
            key = "effective_date" if qualifier == DTP_BENEFIT_BEGIN else "termination_date"
            # A DTP before the first HD is the loop-level date and applies to
            # every coverage line that follows.
            if current_coverage is not None:
                current_coverage[key] = parsed
            if member[key] is None:
                member[key] = parsed

    # Promote the first coverage line into the flat keys, and make sure there is
    # always at least one line so the sync engine has something to write.
    if member["coverages"]:
        first = member["coverages"][0]
        member["plan_code"] = first["plan_code"]
        member["insurance_line_code"] = first["insurance_line_code"]
        if first["effective_date"]:
            member["effective_date"] = first["effective_date"]
        if first["termination_date"]:
            member["termination_date"] = first["termination_date"]
        for coverage in member["coverages"]:
            if coverage["effective_date"] is None:
                coverage["effective_date"] = member["effective_date"]
            if coverage["termination_date"] is None:
                coverage["termination_date"] = member["termination_date"]
            if not coverage["maintenance_type_code"]:
                coverage["maintenance_type_code"] = member["maintenance_type_code"]
    else:
        member["coverages"].append(
            {
                "insurance_line_code": member["insurance_line_code"],
                "plan_code": member["plan_code"],
                "maintenance_type_code": member["maintenance_type_code"],
                "effective_date": member["effective_date"],
                "termination_date": member["termination_date"],
            }
        )

    return member


def custodial_parent_from(loop) -> Optional[dict]:
    """The S3 NM1 block and the address that follows it, when present."""
    segments = getattr(loop, "segments", loop)
    for index, segment in enumerate(segments):
        if segment.name == "NM1" and segment.get(1).strip().upper() == CUSTODIAL_PARENT:
            parent = {
                "last_name": segment.get(3).strip()[:60],
                "first_name": segment.get(4).strip()[:35],
                "address1": "",
                "address2": "",
                "city": "",
                "state": "",
                "postal_code": "",
                "phone": "",
            }
            for following in segments[index + 1:]:
                if following.name == "N3":
                    parent["address1"] = following.get(1).strip()[:55]
                    parent["address2"] = following.get(2).strip()[:55]
                elif following.name == "N4":
                    parent["city"] = following.get(1).strip()[:30]
                    parent["state"] = following.get(2).strip().upper()[:2]
                    parent["postal_code"] = "".join(ch for ch in following.get(3) if ch.isdigit())[:9]
                elif following.name in ("NM1", "INS"):
                    break
            return parent
    return None

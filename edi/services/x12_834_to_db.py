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

from .ssn import NON_SSN_REF_QUALIFIERS, is_structurally_impossible
from .ssn import normalize_ssn as normalize_ssn_value
from .ssn import ssn_from_segments

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
        # Where member_id came from, so a screen can say "sponsor assigned"
        # rather than leaving an operator to guess whether a number is the
        # carrier's or the subscriber number standing in for it.
        "member_id_source": "",
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
        "class_code": "",
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
        # Anything the SSN rules refused, so the caller can log it rather than
        # discovering an empty column three files later.
        "ssn_warnings": [],
    }

    # ---------------------------------------------------------------
    # Part 14: the SSN comes from the qualifiers, not from position.
    #
    # Resolved up front, over the whole loop, because the correct source
    # (NM109 under NM108=34) can appear after segments that used to overwrite
    # it. REF*0F is deliberately not consulted: it is the subscriber number,
    # and on a dependant loop it carries the *subscriber's* value, so reading
    # it as the dependant's SSN gives every child in a family their father's
    # number.
    # ---------------------------------------------------------------
    resolved_ssn, ssn_warning = ssn_from_segments(segments)
    if resolved_ssn:
        member["ssn"] = resolved_ssn
        if is_structurally_impossible(resolved_ssn):
            member["ssn_warnings"].append(
                "SSN ending {last4} is not a number the SSA issues; stored as sent.".format(
                    last4=resolved_ssn[-4:]
                )
            )
    elif ssn_warning:
        member["ssn_warnings"].append(ssn_warning)

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
            qualifier = segment.get(8).strip().upper()
            if qualifier == "34":
                # NM108=34 says NM109 is a Social Security Number. The SSN
                # itself was already resolved above; all that is left is the
                # case where the value under a 34 qualifier is not nine digits,
                # which means the sponsor mislabelled an identifier. Keeping it
                # as member_id preserves the value without pretending it is an
                # SSN.
                #
                # Note what does not happen: a valid SSN is never copied into
                # member_id. That is what used to happen, and member_id is
                # rendered in clear on the roster, in search results and in the
                # admin, so masking the ssn column achieved nothing while nine
                # plaintext digits sat in the column next to it.
                if not normalize_ssn_value(segment.get(9))[0]:
                    member["member_id"] = segment.get(9).strip()[:80]
            elif qualifier in ("ZZ", "MI", "N", "C"):
                # Carrier or sponsor assigned member identifier. Numeric or
                # not, it is not an SSN and must never be treated as one.
                member["member_id"] = segment.get(9).strip()[:80]

        elif name == "REF":
            qualifier = segment.get(1).strip().upper()
            value = segment.get(2).strip()
            if qualifier == REF_SUBSCRIBER_NUMBER:
                # The subscriber number, and only that. It is repeated verbatim
                # on every dependant in the family, which is precisely why it
                # cannot double as an SSN column.
                member["subscriber_number"] = value[:80]
            elif qualifier == REF_GROUP_NUMBER:
                member["group_number"] = value[:50]
            elif qualifier == REF_UNION_LOCAL:
                member["local"] = value[:30]
            elif qualifier in NON_SSN_REF_QUALIFIERS and not member["member_id"]:
                # Recorded as an identifier of last resort so the value is not
                # lost, but never as an SSN.
                if qualifier == "17":
                    member["class_code"] = value[:30]

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

    # ---------------------------------------------------------------
    # The member identifier: what is NOT done here, and why.
    #
    # The reported defect is that the member card shows no Member ID. The cause
    # is that this column is blank on every row for any sponsor that identifies
    # its members by SSN: such a sponsor sends NM108=34 and nothing else, so
    # there is no carrier-assigned identifier in the loop to read.
    #
    # The obvious repair is to fall back to REF*0F, the subscriber number, and
    # it is a trap. REF*0F is not unique per person and is not guaranteed unique
    # per subscriber either - it is whatever the sponsor's extract puts there,
    # repeated verbatim on every member of a family, and some sponsors emit a
    # constant. Writing it into member_id makes it an identity key, because
    # resolve_member_identity and RosterIndex both match on member_id first and
    # match on it before anything else. Two different subscribers sharing a
    # REF*0F would then resolve to the same person and be silently merged, which
    # is a far worse outcome than an empty column and much harder to notice: the
    # roster simply gets smaller.
    #
    # This was not hypothetical. Writing the fallback here collapsed two
    # distinct subscribers into one on the first fixture that shared a REF*0F,
    # and the only reason it surfaced immediately is that the existing test
    # suite happened to build its files that way.
    #
    # So member_id keeps its narrow, honest meaning - a sponsor-assigned
    # identifier or nothing - and the fallback lives at the display layer
    # instead, in MemberSerializer.display_member_id, where it can be shown to
    # an operator and labelled as what it is without ever being matched on.
    # ---------------------------------------------------------------
    if member["member_id"]:
        member["member_id_source"] = "NM109 insured identifier"

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

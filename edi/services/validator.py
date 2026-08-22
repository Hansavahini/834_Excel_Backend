"""
Structural validation of an 834 interchange.

This module was an empty file. Validation is the second box in the stated
workflow, so every upload was going straight from disk into the parser with
nothing between them: a truncated file, a 270 eligibility request, or a PDF
renamed to .x12 would all be accepted and produce an empty or nonsensical
workbook rather than an error the user could act on.

What is checked here is envelope integrity and transaction-set identity, not
the full TR3. Deep code-set validation (is INS02 a valid relationship code, is
DTP03 a real date) belongs in the mapping and load stage where a bad value can
be reported against a named member. What matters at this gate is: can this file
be trusted enough to parse at all, and is it the transaction we think it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

# Envelope pairs: opener -> (closer, control number element on each side)
ENVELOPE_PAIRS = (
    ("ISA", "IEA", 13, 2),
    ("GS", "GE", 6, 2),
    ("ST", "SE", 2, 2),
)

EXPECTED_TRANSACTION_SET = "834"
# 005010X220A1 is the HIPAA mandated 834 implementation. Files quoting anything
# else are not rejected, because sponsors do send 004010 and X220 without the
# A1, but the difference is worth surfacing.
PREFERRED_VERSION = "005010X220A1"


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    segment_count: int = 0
    member_loop_count: int = 0
    transaction_count: int = 0
    is_full_file: bool = None

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "segment_count": self.segment_count,
            "member_loop_count": self.member_loop_count,
            "transaction_count": self.transaction_count,
            "is_full_file": self.is_full_file,
        }


def validate_834(segments: Iterable) -> ValidationResult:
    """
    Walk the segment stream once and report everything wrong with it.

    Deliberately collects rather than raising on the first problem: a user who
    uploads a bad file wants the whole list, not one error per re-upload.
    """
    result = ValidationResult()

    stack = []  # open envelopes: (name, control_number, segment_count_at_open)
    seen_names = set()
    ins_maintenance_codes = set()
    segment_index = 0
    st_segment_start = None

    for segment in segments:
        segment_index += 1
        name = segment.name
        seen_names.add(name)

        if name == "ISA":
            if stack:
                result.errors.append("A second ISA appears before the first IEA closed it.")
            stack.append(("ISA", segment.get(13).strip(), segment_index))

        elif name == "GS":
            if not stack or stack[-1][0] != "ISA":
                result.errors.append("GS appears outside an ISA interchange.")
            stack.append(("GS", segment.get(6).strip(), segment_index))
            version = segment.get(8).strip()
            if version and version != PREFERRED_VERSION:
                result.warnings.append(
                    "GS08 reports version {v}; this converter is built against {p}.".format(
                        v=version, p=PREFERRED_VERSION
                    )
                )

        elif name == "ST":
            set_id = segment.get(1).strip()
            if set_id != EXPECTED_TRANSACTION_SET:
                result.errors.append(
                    "Transaction set is {actual}, not an 834 enrolment file.".format(actual=set_id or "missing")
                )
            stack.append(("ST", segment.get(2).strip(), segment_index))
            st_segment_start = segment_index
            result.transaction_count += 1

        elif name in ("SE", "GE", "IEA"):
            opener = {"SE": "ST", "GE": "GS", "IEA": "ISA"}[name]
            if not stack or stack[-1][0] != opener:
                result.errors.append(
                    "{closer} at segment {pos} has no matching {opener}.".format(
                        closer=name, pos=segment_index, opener=opener
                    )
                )
                continue
            open_name, open_control, open_index = stack.pop()
            close_control = segment.get(2).strip()
            if open_control and close_control and open_control != close_control:
                result.errors.append(
                    "{opener}/{closer} control numbers disagree: {a} then {b}.".format(
                        opener=open_name, closer=name, a=open_control, b=close_control
                    )
                )
            if name == "SE":
                # SE01 counts every segment from ST through SE inclusive.
                declared = segment.get(1).strip()
                actual = segment_index - open_index + 1
                if declared.isdigit() and int(declared) != actual:
                    result.errors.append(
                        "SE01 declares {declared} segments but the transaction contains {actual}. "
                        "The file is probably truncated.".format(declared=int(declared), actual=actual)
                    )
                st_segment_start = None
            elif name == "GE":
                declared = segment.get(1).strip()
                if declared.isdigit() and int(declared) != result.transaction_count:
                    result.warnings.append(
                        "GE01 declares {declared} transaction sets, {actual} were read.".format(
                            declared=int(declared), actual=result.transaction_count
                        )
                    )

        elif name == "INS":
            result.member_loop_count += 1
            code = segment.get(3).strip()
            if code:
                ins_maintenance_codes.add(code)

    result.segment_count = segment_index

    if segment_index == 0:
        result.errors.append("No segments were found in the file.")

    for open_name, _control, open_index in stack:
        result.errors.append(
            "{name} opened at segment {pos} is never closed; the file is truncated.".format(
                name=open_name, pos=open_index
            )
        )

    for required in ("ISA", "GS", "ST", "BGN"):
        if required not in seen_names:
            result.errors.append("Required segment {name} is missing.".format(name=required))

    if result.member_loop_count == 0 and not result.errors:
        result.warnings.append("The file is structurally valid but contains no INS member loops.")

    if ins_maintenance_codes:
        # INS03=030 throughout means an audit/compare full file; anything else
        # means a change-only file. The comparison logic needs to know which.
        result.is_full_file = ins_maintenance_codes == {"030"}

    return result

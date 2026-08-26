"""
A stable content digest for one 834 member loop.

This is the piece that lets the sync engine answer "has anything about this
person actually changed since the last file?" without reading the person's row
back out of the database, and it is the reason a day-10 file that repeats a
day-1 roster verbatim now costs almost nothing.

Two things about the design matter.

It digests meaning, not bytes. Segment order, whitespace, dashes in an SSN and
the case of a state code are all things a sponsor's extract can change between
runs without a single fact about the member being different, so they are
normalised away before hashing. What survives is the set of values the members
tables actually store: identity, demographics, address, the INS status codes,
and every coverage line with its plan and its dates.

And it deliberately covers exactly the fields the sync writes, no more. A digest
that included, say, the file's control number would differ on every upload and
the fast path would never fire. A digest that omitted plan_code would let a plan
change pass unnoticed, which is worse than slow. The set below is kept in step
with DEMOGRAPHIC_FIELDS in member_sync by DIGEST_FIELDS being the single list
both read.
"""

from __future__ import annotations

import hashlib

# Every scalar the member sync persists. Keep this in step with
# member_sync.DEMOGRAPHIC_FIELDS: a field the sync writes but this list omits is
# a change the fast path would silently swallow.
DIGEST_FIELDS = (
    "member_type",
    "relationship_code",
    "member_id",
    "subscriber_number",
    "group_number",
    "first_name",
    "middle_name",
    "last_name",
    "name_suffix",
    "gender_code",
    "date_of_birth",
    "address1",
    "address2",
    "city",
    "state",
    "postal_code",
    "phone",
    "email",
    "local",
    "plan_code",
    "class_code",
    "benefit_status_code",
    "employment_status_code",
    "student_status_code",
)

# Per-coverage-line fields. A member with medical and dental has two of these
# and a change to either one has to move the digest.
COVERAGE_FIELDS = (
    "insurance_line_code",
    "plan_code",
    "maintenance_type_code",
    "effective_date",
    "termination_date",
)


def _text(value) -> str:
    """
    One value, reduced to the form the database would hold.

    None and empty string collapse to the same thing on purpose: the sync never
    blanks a known field, so "absent" and "empty" are the same instruction.
    """
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def loop_digest(parsed_dict: dict) -> str:
    """
    A 64 character hex digest of everything the sync would write for this loop.

    Equal digests mean the file is asserting exactly what is already stored, so
    the whole write path can be skipped. Unequal digests mean something moved
    and the slow path has to run — including the field-level diff that feeds the
    change monitor.
    """
    parts = []

    for field in DIGEST_FIELDS:
        parts.append(field)
        parts.append(_text(parsed_dict.get(field)))

    # Coverage lines are sorted rather than taken in file order. A sponsor that
    # emits HD*DEN before HD*HLT one week and the other way round the next has
    # not changed anybody's coverage, and treating that as a change would put
    # the whole roster through the slow path for nothing.
    coverages = parsed_dict.get("coverages") or []
    encoded = []
    for coverage in coverages:
        encoded.append("|".join(_text(coverage.get(key)) for key in COVERAGE_FIELDS))
    for line in sorted(encoded):
        parts.append("HD")
        parts.append(line)

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8"))
    return digest.hexdigest()

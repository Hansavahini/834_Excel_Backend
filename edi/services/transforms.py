"""
Value transforms, and the cell type each one implies.

Two changes here matter beyond the obvious.

Part 4 asked for MM-DD-YYYY everywhere, so DATE_MDY now produces 08-25-2026
rather than 08/25/2026. The slashed form is kept under its own name because
removing a transform silently reinterprets every saved mapping that referenced
it, and a mapping template is exactly the kind of record that outlives the
person who wrote it.

The larger change is CELL_KIND. A transform used to be a string-to-string
function and nothing else, which is why "apply proper date formatting in Excel"
was not achievable: by the time the workbook writer saw a value it was a string
and indistinguishable from any other string. Each transform now declares what
kind of cell it produces — DATE, TEXT or GENERAL — and the writer uses that to
emit a real date cell with a number format, or a text cell that keeps the
leading zero on 001234567 instead of letting Excel round it off to 1234567.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

DIGITS = re.compile(r"\D")

# How a value should be written into a spreadsheet cell.
KIND_GENERAL = "GENERAL"
KIND_DATE = "DATE"
KIND_TEXT = "TEXT"


# --- date handling ---------------------------------------------------------

def _date_parts(value: str):
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        return value[0:4], value[4:6], value[6:8]
    return None


def to_date(value) -> Optional[date]:
    """
    Best-effort parse of any spelling this system produces or receives.

    Returns None rather than raising. The workbook writer falls back to writing
    the original string when it gets None, so one malformed DTP in one member
    loop costs that one cell rather than the whole conversion.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None

    text = str(value).strip()
    if not text:
        return None

    parts = _date_parts(text)
    if parts:
        year, month, day = parts
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def date_mdy(value: str) -> str:
    """CCYYMMDD to MM-DD-YYYY. The portal's one display format."""
    parsed = to_date(value)
    if parsed is None:
        return value
    return "{m:02d}-{d:02d}-{y:04d}".format(m=parsed.month, d=parsed.day, y=parsed.year)


def date_mdy_slash(value: str) -> str:
    """CCYYMMDD to MM/DD/YYYY. Retained for templates that ask for it by name."""
    parsed = to_date(value)
    if parsed is None:
        return value
    return "{m:02d}/{d:02d}/{y:04d}".format(m=parsed.month, d=parsed.day, y=parsed.year)


def date_iso(value: str) -> str:
    """CCYYMMDD to YYYY-MM-DD."""
    parsed = to_date(value)
    if parsed is None:
        return value
    return parsed.isoformat()


# --- SSN handling ----------------------------------------------------------

def ssn_plain(value: str) -> str:
    """
    Nine digits, no punctuation, leading zeros intact.

    This is the default for an SSN column. Returning "" for anything that is not
    nine digits is deliberate: an eight digit value in an SSN column is not a
    slightly wrong SSN, it is a different field that has been mapped by mistake,
    and printing it would make the mistake look like data.
    """
    digits = DIGITS.sub("", value or "")
    return digits if len(digits) == 9 else ""


def ssn_dashed(value: str) -> str:
    digits = DIGITS.sub("", value or "")
    if len(digits) != 9:
        return value
    return "{a}-{b}-{c}".format(a=digits[:3], b=digits[3:5], c=digits[5:])


def ssn_last4(value: str) -> str:
    digits = DIGITS.sub("", value or "")
    if len(digits) < 4:
        return ""
    return "XXX-XX-{last4}".format(last4=digits[-4:])


def phone(value: str) -> str:
    digits = DIGITS.sub("", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return value
    return "({a}) {b}-{c}".format(a=digits[:3], b=digits[3:6], c=digits[6:])


TRANSFORMS = {
    "NONE": lambda value: value,
    "DATE_MDY": date_mdy,
    "DATE_MDY_SLASH": date_mdy_slash,
    "DATE_ISO": date_iso,
    "SSN": ssn_plain,
    "SSN_PLAIN": ssn_plain,
    "SSN_DASHED": ssn_dashed,
    "SSN_LAST4": ssn_last4,
    "UPPER": lambda value: (value or "").upper(),
    "LOWER": lambda value: (value or "").lower(),
    "TITLE": lambda value: (value or "").title(),
    "PHONE": phone,
    # Seed data written before the choice list settled uses these names.
    "PHONE_DASHED": phone,
    "TEXT": lambda value: "" if value is None else str(value),
}

# What kind of cell each transform produces. Anything not listed is GENERAL.
CELL_KINDS = {
    "DATE_MDY": KIND_DATE,
    "DATE_MDY_SLASH": KIND_DATE,
    "DATE_ISO": KIND_DATE,
    "SSN": KIND_TEXT,
    "SSN_PLAIN": KIND_TEXT,
    "SSN_DASHED": KIND_TEXT,
    "SSN_LAST4": KIND_TEXT,
    "TEXT": KIND_TEXT,
}


def cell_kind(name: str) -> str:
    return CELL_KINDS.get((name or "NONE").upper(), KIND_GENERAL)


def apply_transform(value: str, name: str) -> str:
    handler = TRANSFORMS.get((name or "NONE").upper())
    if handler is None:
        return value
    try:
        return handler(value)
    except Exception:  # a bad value must never abort the run
        return value

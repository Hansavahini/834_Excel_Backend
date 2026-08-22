"""
Value transforms.

MappingDetail has offered a Transform choice list since the model was written,
but nothing implemented it, so a column mapped as DATE_MDY still emitted the raw
CCYYMMDD from the file. These are the implementations.

Every transform is total: given a value it cannot handle it returns the value
unchanged rather than raising, because one malformed date in one member loop
should not abort a 27,000 row conversion.
"""

from __future__ import annotations

import re

DIGITS = re.compile(r"\D")


def _date_parts(value: str):
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return value[0:4], value[4:6], value[6:8]
    return None


def date_mdy(value: str) -> str:
    """CCYYMMDD to MM/DD/YYYY."""
    parts = _date_parts(value)
    if not parts:
        return value
    year, month, day = parts
    return "{m}/{d}/{y}".format(m=month, d=day, y=year)


def date_iso(value: str) -> str:
    """CCYYMMDD to YYYY-MM-DD."""
    parts = _date_parts(value)
    if not parts:
        return value
    year, month, day = parts
    return "{y}-{m}-{d}".format(y=year, m=month, d=day)


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
    "DATE_ISO": date_iso,
    "SSN_DASHED": ssn_dashed,
    "SSN_LAST4": ssn_last4,
    "UPPER": lambda value: (value or "").upper(),
    "LOWER": lambda value: (value or "").lower(),
    "TITLE": lambda value: (value or "").title(),
    "PHONE": phone,
    # Seed data written before the choice list settled uses these names.
    "PHONE_DASHED": phone,
}


def apply_transform(value: str, name: str) -> str:
    handler = TRANSFORMS.get((name or "NONE").upper())
    if handler is None:
        return value
    try:
        return handler(value)
    except Exception:  # a bad value must never abort the run
        return value

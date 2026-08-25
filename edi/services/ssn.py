"""
Social Security Numbers: one place that decides what one is.

Part 14 of the brief describes three separate failures that all trace back to
the same missing idea — that an SSN is a specific thing with a specific source,
rather than "whatever nine-ish characters turned up nearby".

  1. Extraction took REF*0F as the SSN. In the 834 TR3, REF01=0F is the
     subscriber number. Sponsors frequently populate it with the subscriber's
     SSN, which is why the mistake survives testing: on a subscriber loop it
     usually looks right. On a dependant loop it is not the dependant's SSN at
     all, it is the *subscriber's* number, repeated on every child in the
     family. So a family of four produced four members sharing one SSN, and
     de-duplicating on SSN would then have merged the children into their
     father. The sample files in this repository show exactly this: LINDA and
     PATRICIA SMITH both carry REF*0F*100000000, which belongs to JOHN.

  2. Storage accepted whatever it was given, including eight digits, eleven
     digits and 123-45-6789, so the same person matched himself only when the
     punctuation happened to agree.

  3. Excel received a numeric-looking string, decided it was a number, and ate
     the leading zero. 001234567 became 1234567 in the workbook a client
     received, and nothing anywhere reported an error.

The correct source, in order of authority:

  * NM108 = 34 means NM109 is a Social Security Number. This is the one the
    TR3 actually designates and it is per-person, so it is right for
    dependants as well as subscribers.
  * REF01 = SY is the Social Security Number qualifier where a partner sends
    it as a REF.
  * Nothing else. Not REF*0F (subscriber number), not REF*1L (group or policy
    number), not REF*17 (client reporting category), not REF*23 (client
    number), not NM109 under NM108 = MI or ZZ, which are member identifiers a
    carrier assigned and are not SSNs however numeric they look.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

DIGITS_ONLY = re.compile(r"\D")

SSN_LENGTH = 9

# NM108 identification code qualifier meaning "Social Security Number".
NM1_SSN_QUALIFIER = "34"

# REF01 qualifiers. Only SY is an SSN. The rest are listed by name so the next
# person reading this file does not have to guess why 0F is excluded.
REF_SSN_QUALIFIER = "SY"
REF_SUBSCRIBER_NUMBER = "0F"
REF_GROUP_NUMBER = "1L"
REF_CLIENT_REPORTING_CATEGORY = "17"
REF_CLIENT_NUMBER = "23"
REF_POLICY_NUMBER = "IG"

NON_SSN_REF_QUALIFIERS = frozenset(
    {
        REF_SUBSCRIBER_NUMBER,
        REF_GROUP_NUMBER,
        REF_CLIENT_REPORTING_CATEGORY,
        REF_CLIENT_NUMBER,
        REF_POLICY_NUMBER,
        "6O",
        "ZZ",
        "DX",
        "F6",
        "3H",
        "QQ",
        "1W",
        "49",
        "60",
        "ABB",
        "D3",
        "LU",
    }
)


def clean_ssn(value) -> str:
    """
    Strip everything that is not a digit.

    123-45-6789, '123 45 6789' and ' 123456789 ' are the same nine digits and a
    trading partner will send all three spellings for the same person inside one
    quarter. Anything left over is returned as-is for the validator to judge;
    this function never guesses or truncates.
    """
    if value is None:
        return ""
    return DIGITS_ONLY.sub("", str(value))


def is_valid_ssn(value) -> bool:
    """Exactly nine digits. Leading zeros are digits and count."""
    digits = clean_ssn(value)
    return len(digits) == SSN_LENGTH and digits.isdigit()


def is_structurally_impossible(digits: str) -> bool:
    """
    Numbers the Social Security Administration never issues.

    Area 000, area 666, areas 900-999, group 00 and serial 0000 are all
    unassignable. This is a warning signal, not a rejection: placeholder SSNs
    such as 000000000 and 123456789 are common in test extracts and a real file
    should not be refused because one member carries one. Callers log it.
    """
    if len(digits) != SSN_LENGTH:
        return True
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area[0] == "9":
        return True
    if group == "00" or serial == "0000":
        return True
    return False


def normalize_ssn(value) -> Tuple[str, Optional[str]]:
    """
    Return (nine_digits, error_message).

    A value that cannot be an SSN comes back as ("", reason) rather than as a
    truncated or padded near-miss, because a near-miss is worse than a blank:
    it matches nobody, de-duplicates against nothing, and looks correct in a
    spreadsheet.
    """
    if value in (None, ""):
        return "", None

    raw = str(value).strip()
    digits = clean_ssn(raw)

    if not digits:
        return "", "'{v}' contains no digits and cannot be an SSN.".format(v=raw[:32])
    if len(digits) < SSN_LENGTH:
        return "", "'{v}' has {n} digits; an SSN has nine.".format(v=raw[:32], n=len(digits))
    if len(digits) > SSN_LENGTH:
        return "", "'{v}' has {n} digits; an SSN has nine.".format(v=raw[:32], n=len(digits))

    return digits, None


def format_ssn(value) -> str:
    """
    The nine digit string an Excel cell should hold, leading zeros intact.

    Returned as text on purpose. The workbook writer pairs this with an explicit
    text number format, because openpyxl will happily store '001234567' and
    Excel will still display 1234567 unless the cell says otherwise.
    """
    digits = clean_ssn(value)
    if len(digits) != SSN_LENGTH:
        return ""
    return digits


def mask_ssn(value) -> str:
    digits = clean_ssn(value)
    if len(digits) < 4:
        return ""
    return "XXX-XX-{last4}".format(last4=digits[-4:])


def ssn_from_segments(segments) -> Tuple[str, Optional[str]]:
    """
    Pull the SSN out of one member loop, using the qualifiers rather than luck.

    Returns (nine_digits_or_blank, warning_or_None). The warning is worth
    surfacing: "REF*0F held something that looks like an SSN but 0F is the
    subscriber number, so it was not used" is exactly the sentence somebody
    needs when a column comes back empty and they are certain the data is there.
    """
    fallback = ""

    for segment in segments:
        name = getattr(segment, "name", "")

        if name == "NM1":
            # Only the insured NM1 describes the person this loop is about.
            if segment.get(1).strip().upper() != "IL":
                continue
            if segment.get(8).strip() != NM1_SSN_QUALIFIER:
                continue
            digits, error = normalize_ssn(segment.get(9))
            if digits:
                return digits, None
            if error:
                return "", "NM109 under NM108=34: {e}".format(e=error)

        elif name == "REF":
            qualifier = segment.get(1).strip().upper()
            if qualifier == REF_SSN_QUALIFIER:
                digits, error = normalize_ssn(segment.get(2))
                if digits:
                    fallback = fallback or digits
                elif error:
                    return "", "REF*SY: {e}".format(e=error)

    if fallback:
        return fallback, None
    return "", None

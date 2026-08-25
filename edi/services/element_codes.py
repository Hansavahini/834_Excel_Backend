"""
One canonical spelling for a segment/element pair.

The front end shows NM1-03 because that is how an implementation guide prints
it and how an analyst reads it. The backend resolver needs NM103, because it
strips the segment id off the front and parses what is left as the element
position. Those two conventions met in the middle of the mapping payload and
the resolver got "-03", which is not a number, so the rule silently produced an
empty column: no error, no warning, just a blank in the workbook.

Canonical form here is NM103. Everything that accepts a mapping rule normalises
through this module before it touches the database or the resolver, so the API
is tolerant of NM1-03, NM1_03, NM1 03, NM1-3 and a bare 3, while exactly one
spelling is ever stored.
"""

from __future__ import annotations

import re
from typing import Optional

SEPARATORS = re.compile(r"[\s\-_.:]+")
TRAILING_DIGITS = re.compile(r"(\d+)$")


def normalize_segment(segment: str) -> str:
    """NM1, ref, 'dmg ' all become the segment id as X12 spells it."""
    return SEPARATORS.sub("", str(segment or "")).strip().upper()


def normalize_element(element: str, segment: str = "") -> str:
    """
    Return the canonical element code, e.g. NM103.

    Accepts the segment separately so a caller can pass just a position. When
    the element cannot be interpreted it is returned stripped and uppercased
    rather than mangled, and the caller decides whether that is an error.
    """
    segment = normalize_segment(segment)
    raw = SEPARATORS.sub("", str(element or "")).strip().upper()

    if not raw:
        return ""

    # A bare position: "3", "03" with the segment supplied separately.
    if raw.isdigit():
        return "{seg}{pos:02d}".format(seg=segment, pos=int(raw)) if segment else raw

    if segment and raw.startswith(segment):
        tail = raw[len(segment):]
        if tail.isdigit():
            return "{seg}{pos:02d}".format(seg=segment, pos=int(tail))
        return raw

    # No segment given, or the element names a different segment than the rule
    # claims. Split on the trailing digits so REF2 still normalises to REF02.
    match = TRAILING_DIGITS.search(raw)
    if match:
        head = raw[: match.start()]
        return "{seg}{pos:02d}".format(seg=head, pos=int(match.group(1)))

    return raw


def element_position(element: str, segment: str = "") -> Optional[int]:
    """
    1-based element position inside its segment, or None.

    NM103 -> 3. Tolerates the hyphenated form so a rule that escaped
    normalisation somewhere upstream still resolves rather than going blank.
    """
    segment = normalize_segment(segment)
    canonical = normalize_element(element, segment)
    if not canonical:
        return None

    # Segment first, always. A greedy trailing-digit match on NM103 finds "103"
    # and leaves "NM", which does not equal NM1 and would read as a mismatch.
    if segment and canonical.startswith(segment):
        tail = canonical[len(segment):]
        return int(tail) if tail.isdigit() else None

    match = TRAILING_DIGITS.search(canonical)
    if not match:
        return None
    head = canonical[: match.start()]
    if segment and head and head != segment:
        # The element belongs to a different segment than the rule names.
        return None
    return int(match.group(1))


def normalize_rule_codes(rule: dict) -> dict:
    """
    Normalise every code-bearing field on a mapping rule dict, in place.

    Covers the qualifier as well as the element: REF-01 is exactly as likely to
    arrive hyphenated as REF-02, and a qualifier that fails to resolve turns a
    precise rule into a first-match-wins rule without saying so.
    """
    segment = normalize_segment(rule.get("segment", ""))
    rule["segment"] = segment
    rule["element"] = normalize_element(rule.get("element", ""), segment)
    if rule.get("qualifier_element"):
        rule["qualifier_element"] = normalize_element(rule["qualifier_element"], segment)
    return rule

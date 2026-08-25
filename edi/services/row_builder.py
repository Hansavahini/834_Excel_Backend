"""
Turn member loops into Excel rows using the mapping rules.

The previous builder matched on (segment, element) only, took the first hit and
ignored the occurrence field even though both the parser and the mapping
serializer carried one. In an 834 that is not close enough. NM1 appears for the
insured, the sponsor, the payer and the custodial parent; DTP03 is a begin date
when DTP01 is 348 and a termination date when it is 349; REF02 is an SSN under
REF01=0F and a group number under 1L. Matching on the element alone means every
one of those columns gets whichever instance came first in the file.

Resolution order here is qualifier, then occurrence, then position.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from .element_codes import element_position, normalize_element, normalize_segment
from .transforms import apply_transform, cell_kind

logger = logging.getLogger("edi.row_builder")


def _normalise_rule(rule) -> dict:
    """
    Accept either a MappingDetail instance or the plain dict the API takes, so
    the same builder serves a saved template and an ad-hoc request body.
    """
    if isinstance(rule, dict):
        segment = normalize_segment(rule.get("segment") or "")
        return {
            "excel_column": rule.get("excel_column", ""),
            "segment": segment,
            "element": normalize_element(rule.get("element") or "", segment),
            "qualifier_element": normalize_element(
                rule.get("qualifier_element") or "", segment
            ),
            "qualifier_value": rule.get("qualifier_value") or "",
            "component_index": rule.get("component_index"),
            "occurrence": rule.get("occurrence") or 1,
            "applies_to": rule.get("applies_to") or "BOTH",
            "transform": rule.get("transform") or "NONE",
            "default_value": rule.get("default_value") or "",
            "is_required": bool(rule.get("is_required")),
        }
    model_segment = normalize_segment(rule.segment_element.segment_name)
    return {
        "excel_column": rule.excel_column,
        "segment": model_segment,
        "element": normalize_element(rule.segment_element.element_code, model_segment),
        "qualifier_element": normalize_element(
            rule.qualifier_element or "", model_segment
        ),
        "qualifier_value": rule.qualifier_value or "",
        "component_index": rule.component_index,
        "occurrence": rule.occurrence or 1,
        "applies_to": rule.applies_to,
        "transform": rule.transform,
        "default_value": rule.default_value or "",
        "is_required": rule.is_required,
    }


def _element_index(element_code: str, segment_name: str) -> Optional[int]:
    """
    NM103 in segment NM1 is element index 3.

    Delegates to element_codes so the hyphenated spelling resolves too. The
    previous version sliced the segment id off the front and int()'d the rest,
    which turned NM1-03 into "-03" and returned None, and a None index means
    resolve() returns "" for every row in the file.
    """
    return element_position(element_code, segment_name)


def resolve(rule: dict, segments: List) -> str:
    """Find the value for one mapping rule inside one member loop."""
    index = _element_index(rule["element"], rule["segment"])
    if index is None:
        return ""

    qualifier_element = rule["qualifier_element"]
    qualifier_value = rule["qualifier_value"]
    qualifier_index = (
        _element_index(qualifier_element, rule["segment"]) if qualifier_element else None
    )

    matches = 0
    for segment in segments:
        if segment.name != rule["segment"]:
            continue
        if qualifier_index is not None:
            if segment.get(qualifier_index).strip().upper() != qualifier_value.strip().upper():
                continue
        matches += 1
        if matches < rule["occurrence"]:
            continue
        if rule["component_index"]:
            return segment.component(index, rule["component_index"])
        return segment.get(index)
    return ""


def build_row(
    loop,
    rules: List[dict],
    header_segments: Optional[List] = None,
    subscriber_loop=None,
) -> dict:
    """
    Build one Excel row from one member loop.

    subscriber_loop is the most recent subscriber loop seen before this one.
    On a dependent row, rules scoped applies_to=SUB resolve against it, so the
    LAST NAME / FIRST NAME / SSN / SEX / DOB and address columns at the front
    of the sheet carry the subscriber's values on every row of the family —
    which is how the flat roster layout identifies whose dependent each DEP
    row is. They used to come out blank on dependent rows, leaving a sheet
    where a dependent could not be tied to a subscriber without counting rows.
    """
    row: dict = {}
    warnings: List[str] = []
    applies = loop.applies_to  # SUB or DEP

    for rule in rules:
        if rule["applies_to"] not in ("BOTH", applies):
            # A SUB-scoped column on a dependent row is filled from that
            # dependent's subscriber. A DEP-scoped column on a subscriber row
            # stays blank, as before.
            if (
                applies == "DEP"
                and rule["applies_to"] == "SUB"
                and subscriber_loop is not None
            ):
                value = resolve(rule, subscriber_loop.segments)
                row[rule["excel_column"]] = apply_transform(value, rule["transform"])
            else:
                row[rule["excel_column"]] = ""
            continue

        value = resolve(rule, loop.segments)
        if not value and header_segments:
            # A few columns legitimately come from the file header, e.g. the
            # sponsor name in loop 1000A. Fall back to it rather than blank.
            value = resolve(rule, header_segments)
        if not value:
            value = rule["default_value"]
            if rule["is_required"] and not value:
                warnings.append(
                    "Loop {loop}: required column '{col}' had no {element}.".format(
                        loop=loop.loop_id, col=rule["excel_column"], element=rule["element"]
                    )
                )

        row[rule["excel_column"]] = apply_transform(value, rule["transform"])

    if warnings:
        row.setdefault("__warnings__", []).extend(warnings)
    return row


def column_kinds(mappings: List) -> dict:
    """
    excel_column -> DATE | TEXT | GENERAL, derived from each rule's transform.

    The workbook writer cannot work this out for itself: by the time it sees a
    value, "19600115" and "PPO-GOLD" are both strings. The mapping is the only
    place that knows DOB is a date and SSN must never be treated as a number,
    so the mapping is where the answer comes from.
    """
    kinds: dict = {}
    for rule in mappings:
        normalised = _normalise_rule(rule)
        column = normalised["excel_column"]
        if not column:
            continue
        kinds[column] = cell_kind(normalised["transform"])
    return kinds


def iter_excel_rows(loops: Iterable, mappings: List, header_segments: Optional[List] = None):
    """
    Stream rows so a 27,000 member file never has 27,000 dicts alive at once.

    The current subscriber loop is carried forward as the stream advances. An
    834 lists each subscriber followed by that subscriber's dependents, so the
    most recently seen subscriber is, by the structure of the transaction, the
    subscriber every following dependent belongs to — until the next INS*Y
    starts a new family. Holding one loop is all the memory this costs.
    """
    rules = [_normalise_rule(rule) for rule in mappings]
    current_subscriber = None

    for loop in loops:
        if loop.is_subscriber:
            current_subscriber = loop
        yield build_row(
            loop,
            rules,
            header_segments=header_segments,
            subscriber_loop=current_subscriber,
        )


def build_excel_rows(loops, mappings, header_segments: Optional[List] = None) -> List[dict]:
    """
    Materialised form, keeping the original public name and argument order.

    Accepts a ParsedFile, a list of MemberLoop, or the legacy list of
    {"loop_id", "data"} dicts. The last of these raises, because the flat dict
    form cannot carry the qualifier information the rules now need.
    """
    if hasattr(loops, "loops"):
        header_segments = header_segments or loops.header
        loops = loops.loops

    loops = list(loops)
    if loops and isinstance(loops[0], dict):
        raise TypeError(
            "build_excel_rows() now takes MemberLoop objects from extract_loops(). "
            "The old {'loop_id', 'data'} dicts cannot express qualifier matching."
        )

    return list(iter_excel_rows(loops, mappings, header_segments=header_segments))



def collect_warnings(rows: List[dict]) -> List[str]:
    """Pull the per-row warnings out so they can be stored on ConversionHistory."""
    warnings: List[str] = []
    for row in rows:
        warnings.extend(row.pop("__warnings__", []))
    return warnings

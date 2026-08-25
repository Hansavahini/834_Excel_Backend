"""
Mapping persistence, with versions that stay true after the fact.

The store used to be a module-level Python list, which meant a rule saved
through one gunicorn worker was invisible to the next request, every rule
vanished on restart, and none of it reached the MappingTemplate /
MappingDetail / SegmentElement tables that were already designed, migrated and
wired into the admin.

The second problem, fixed here, was versioning. The template carried a version
number that nothing ever incremented: edits landed on the same row, so a
ConversionHistory that recorded "mapping version 1" was pointing at whatever
version 1 had been edited into since. An auditor asking "what rules produced
this workbook" got an answer that was true only by accident.

So a version is immutable once used. save_mapping writes to the newest unlocked
version of a template; if that version has been locked by a completed
conversion, it is cloned to version n+1 and the edit lands there. Nothing that
history refers to is ever rewritten.
"""

from __future__ import annotations

from typing import List, Optional

from django.db import transaction
from django.utils import timezone

from mapping.models import MappingDetail, MappingTemplate, SegmentElement

from .element_codes import normalize_element, normalize_segment


def _segment_element(segment: str, element: str) -> SegmentElement:
    """
    Look the pair up in the dictionary, creating an inactive placeholder when a
    sponsor sends something the seeded TR3 list does not have. Better a flagged
    unknown element than a rejected mapping the user cannot save.
    """
    segment = normalize_segment(segment)
    element = normalize_element(element, segment)
    obj = SegmentElement.objects.filter(
        segment_name=segment, element_code=element
    ).first()
    if obj:
        return obj
    return SegmentElement.objects.create(
        segment_name=segment,
        element_code=element,
        description="Added from a mapping rule; confirm against the implementation guide.",
        is_active=False,
    )


def _clone_details(source: MappingTemplate, target: MappingTemplate) -> None:
    """Copy every rule onto the new version, so an edit is a delta not a reset."""
    for detail in source.details.all():
        MappingDetail.objects.create(
            mapping_template=target,
            excel_column=detail.excel_column,
            column_order=detail.column_order,
            segment_element=detail.segment_element,
            qualifier_element=detail.qualifier_element,
            qualifier_value=detail.qualifier_value,
            component_index=detail.component_index,
            occurrence=detail.occurrence,
            applies_to=detail.applies_to,
            transform=detail.transform,
            default_value=detail.default_value,
            is_required=detail.is_required,
        )


def writable_template(owner, template_name: str = "Default", client=None) -> MappingTemplate:
    """
    The version an edit may be written to.

    Returns the newest version of the named template when it is still unlocked,
    otherwise a fresh clone at the next version number. Never mutates a version
    a completed conversion has referred to.
    """
    latest = (
        MappingTemplate.objects.filter(
            owner=owner, client=client, mapping_name=template_name
        )
        .order_by("-version")
        .first()
    )

    if latest is None:
        return MappingTemplate.objects.create(
            owner=owner,
            client=client,
            mapping_name=template_name,
            version=1,
            description="Created from the mapping API.",
        )

    if not latest.is_locked:
        return latest

    successor = MappingTemplate.objects.create(
        owner=owner,
        client=client,
        mapping_name=template_name,
        version=latest.version + 1,
        description=(
            "Version {n} of {name}, cloned because version {prev} had already been "
            "used by a completed conversion."
        ).format(n=latest.version + 1, name=template_name, prev=latest.version),
        is_default=False,
        is_active=True,
    )
    _clone_details(latest, successor)
    if latest.column_layout:
        MappingTemplate.objects.filter(pk=successor.pk).update(
            column_layout=latest.column_layout
        )
        successor.column_layout = latest.column_layout

    # The default flag follows the newest editable version, so the next
    # conversion picks up the edit without the caller naming a template id.
    if latest.is_default:
        MappingTemplate.objects.filter(pk=latest.pk).update(is_default=False)
        MappingTemplate.objects.filter(pk=successor.pk).update(is_default=True)
        successor.refresh_from_db()

    return successor


def lock_template(template: Optional[MappingTemplate]) -> None:
    """Freeze a version the moment a conversion has finished using it."""
    if template is None or template.locked_at is not None:
        return
    MappingTemplate.objects.filter(pk=template.pk, locked_at__isnull=True).update(
        locked_at=timezone.now()
    )


def rule_fingerprint(rules) -> str:
    """
    A stable digest of a rule set, whatever shape the rules arrive in.

    Accepts MappingDetail rows or the plain dicts the API takes, and produces
    the same digest for the same mapping either way. That equivalence is the
    whole point: it lets save_mappings() tell "the user edited a dropdown" apart
    from "the user pressed Convert again without changing anything".
    """
    import hashlib
    import json

    normalised = []
    for rule in rules:
        if isinstance(rule, dict):
            segment = normalize_segment(rule.get("segment") or "")
            normalised.append(
                {
                    "excel_column": rule.get("excel_column", ""),
                    "column_order": rule.get("column_order") or 0,
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
            )
        else:
            segment = normalize_segment(rule.segment_element.segment_name)
            normalised.append(
                {
                    "excel_column": rule.excel_column,
                    "column_order": rule.column_order or 0,
                    "segment": segment,
                    "element": normalize_element(
                        rule.segment_element.element_code, segment
                    ),
                    "qualifier_element": normalize_element(
                        rule.qualifier_element or "", segment
                    ),
                    "qualifier_value": rule.qualifier_value or "",
                    "component_index": rule.component_index,
                    "occurrence": rule.occurrence or 1,
                    "applies_to": rule.applies_to,
                    "transform": rule.transform,
                    "default_value": rule.default_value or "",
                    "is_required": bool(rule.is_required),
                }
            )

    canonical = json.dumps(
        sorted(normalised, key=lambda item: str(item["excel_column"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@transaction.atomic
def save_mappings(
    rules, owner, template_name: str = "Default", client=None, columns=None
) -> dict:
    """
    Persist a whole rule set in one go, and only when it has actually changed.

    This replaces a loop over save_mapping(), which had two problems that only
    showed up under real use.

    The first was version inflation. A completed conversion locks the template
    version it used, so the next edit clones to version n+1 - correct, and the
    reason history stays true. But the Convert button saves the mapping before
    every run whether or not anything was edited, so three identical clicks
    produced versions 1, 2, 3 and 4, each a full copy of all thirty rules,
    growing the mapping tables without a single rule ever changing. Comparing
    fingerprints first means a version is minted when the mapping is different
    and not otherwise.

    The second was that a column the user cleared stayed mapped. save_mapping()
    only ever wrote rows; nothing deleted the MappingDetail for a column whose
    segment had been set back to "Select...", so the workbook kept filling a
    column the screen showed as unmapped. The incoming set is now the whole
    truth for the template: rules not in it are removed.
    """
    # Fill in the column order before anything is compared.
    #
    # The API leaves column_order null when the caller does not send one, and
    # the stored rows always have 1..n. Fingerprinting the raw incoming rules
    # therefore never matched what was stored, so "has the mapping changed?"
    # answered yes every time and a version was minted on every save - the exact
    # inflation this function exists to prevent. Position in the list is the
    # order the caller meant; make it explicit first, then compare.
    ordered = []
    for index, rule in enumerate(list(rules or []), start=1):
        payload = dict(rule)
        if not payload.get("column_order"):
            payload["column_order"] = index
        ordered.append(payload)
    rules = ordered

    incoming = rule_fingerprint(rules)

    latest = (
        MappingTemplate.objects.filter(
            owner=owner, client=client, mapping_name=template_name
        )
        .order_by("-version")
        .first()
    )

    # The column layout, which outlives any individual rule. Callers that know
    # the whole layout (the conversion endpoint, which is sent `columns`) pass
    # it; callers that only send rules keep whatever layout is already stored
    # and gain any column a new rule introduces. Either way, un-mapping a
    # column narrows its rule and never removes the column.
    layout = list(columns or [])
    if not layout:
        layout = list((latest.column_layout if latest else None) or [])
    for rule in rules:
        if rule["excel_column"] not in layout:
            layout.append(rule["excel_column"])

    layout_changed = bool(latest) and list(latest.column_layout or []) != layout

    if (
        latest is not None
        and not layout_changed
        and rule_fingerprint(
            list(latest.details.select_related("segment_element").all())
        )
        == incoming
    ):
        # Byte-for-byte the mapping that is already stored. Returning the
        # existing version is not a shortcut; minting a new one would be a lie
        # about when the mapping last changed.
        return {
            "template_id": latest.id,
            "template": latest.mapping_name,
            "version": latest.version,
            "locked": bool(latest.locked_at),
            "changed": False,
            "columns": len(rules),
        }

    template = writable_template(owner, template_name, client=client)

    keep = set()
    for rule in rules:
        detail = _write_detail(rule, template)
        keep.add(detail.id)

    # A column the user unmapped must stop being mapped. Deleting on the new
    # version only, never on a locked one, so nothing a history row points at
    # is edited.
    template.details.exclude(id__in=keep).delete()

    MappingTemplate.objects.filter(pk=template.pk).update(
        column_layout=layout, updated_at=timezone.now()
    )
    template.column_layout = layout

    return {
        "template_id": template.id,
        "template": template.mapping_name,
        "version": template.version,
        "locked": bool(template.locked_at),
        "changed": True,
        "columns": len(rules),
    }


def _write_detail(rule: dict, template: MappingTemplate) -> MappingDetail:
    """One rule onto one template version. Shared by save_mapping and save_mappings."""
    order = rule.get("column_order")
    if not order:
        existing = template.details.filter(excel_column=rule["excel_column"]).first()
        if existing:
            order = existing.column_order
        else:
            highest = (
                template.details.order_by("-column_order")
                .values_list("column_order", flat=True)
                .first()
            )
            order = (highest or 0) + 1

    segment = normalize_segment(rule["segment"])

    detail, _created = MappingDetail.objects.update_or_create(
        mapping_template=template,
        excel_column=rule["excel_column"],
        defaults={
            "column_order": order,
            "segment_element": _segment_element(segment, rule["element"]),
            "qualifier_element": normalize_element(
                rule.get("qualifier_element", "") or "", segment
            ),
            "qualifier_value": rule.get("qualifier_value", "") or "",
            "component_index": rule.get("component_index"),
            "occurrence": rule.get("occurrence") or 1,
            "applies_to": rule.get("applies_to") or "BOTH",
            "transform": rule.get("transform") or "NONE",
            "default_value": rule.get("default_value", "") or "",
            "is_required": bool(rule.get("is_required")),
        },
    )
    return detail


@transaction.atomic
def save_mapping(rule: dict, owner, template_name: str = "Default", client=None) -> dict:
    """
    Persist one column rule against a user's writable template version.

    Kept for callers that genuinely edit a single column. Anything saving a
    whole screenful should use save_mappings(), which compares the set against
    what is stored and does not mint a version when nothing changed.
    """
    template = writable_template(owner, template_name, client=client)
    detail = _write_detail(rule, template)

    return {
        "id": detail.id,
        "template_id": template.id,
        "template": template.mapping_name,
        "version": template.version,
        "excel_column": detail.excel_column,
        "column_order": detail.column_order,
        "segment": detail.segment,
        "element": detail.element,
        "qualifier_element": detail.qualifier_element,
        "qualifier_value": detail.qualifier_value,
        "occurrence": detail.occurrence,
        "applies_to": detail.applies_to,
        "transform": detail.transform,
    }


def get_template(owner, template_id: Optional[int] = None, client=None) -> Optional[MappingTemplate]:
    """Named template, else the user's default, else their most recent."""
    queryset = MappingTemplate.objects.filter(owner=owner, is_active=True)
    if client is not None:
        queryset = queryset.filter(client=client)
    if template_id:
        return queryset.filter(pk=template_id).first()
    return (
        queryset.filter(is_default=True).order_by("-version").first()
        or queryset.order_by("-updated_at", "-version").first()
    )


def get_mappings(owner, template_id: Optional[int] = None, client=None) -> List[MappingDetail]:
    template = get_template(owner, template_id, client=client)
    if not template:
        return []
    return list(
        template.details.select_related("segment_element").order_by("column_order")
    )


def headers_for(details: List[MappingDetail]) -> List[str]:
    """Column headers in template order, so the workbook matches the mapping."""
    return [detail.excel_column for detail in details]


def layout_for(template, details) -> list:
    """
    The whole column grid: every column in order, each with its rule or blank.

    The mapping screen shows a fixed layout of columns, some of which are
    intentionally unmapped. Returning only the columns that have rules is what
    made an un-mapped column vanish from the screen entirely, leaving no way to
    map it again short of resetting every other row.
    """
    by_column = {detail.excel_column: detail for detail in details}
    order = list((getattr(template, "column_layout", None) or []))
    for detail in details:
        if detail.excel_column not in order:
            order.append(detail.excel_column)

    rendered = []
    for index, column in enumerate(order, start=1):
        detail = by_column.get(column)
        if detail is None:
            rendered.append(
                {
                    "excel_column": column,
                    "column_order": index,
                    "segment": "",
                    "element": "",
                    "qualifier_element": "",
                    "qualifier_value": "",
                    "occurrence": 1,
                    "applies_to": "BOTH",
                    "transform": "NONE",
                }
            )
            continue
        rendered.append(
            {
                "excel_column": detail.excel_column,
                "column_order": detail.column_order,
                "segment": detail.segment,
                "element": detail.element,
                "qualifier_element": detail.qualifier_element,
                "qualifier_value": detail.qualifier_value,
                "occurrence": detail.occurrence,
                "applies_to": detail.applies_to,
                "transform": detail.transform,
            }
        )
    return rendered

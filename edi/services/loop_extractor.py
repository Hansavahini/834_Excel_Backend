"""
Split a segment stream into the header and one loop per INS.

Two bugs lived here. First, the extractor was fed a flat list of *elements*, so
it flushed a loop on every element whose segment happened to be INS. One INS
segment carries up to eight elements, so a 26,734 member file produced 166,074
"loops", each holding a single element, with the member's real segments landing
in whichever loop the last INS element had opened. Every downstream row was
therefore wrong, and wrong quietly.

Second, the segments before the first INS (ISA, GS, ST, BGN, the N1 sponsor and
payer blocks) were emitted as loop 1. That put a phantom member at the top of
every workbook whose only populated column was whichever header element the
mapping happened to collide with.

Header segments are now returned separately: they are file-level facts, not a
member, and some of them are genuinely useful on every row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List


@dataclass
class MemberLoop:
    """One 2000 loop: the INS segment and everything up to the next INS."""

    loop_id: int
    segments: List = field(default_factory=list)

    @property
    def ins(self):
        return self.segments[0] if self.segments and self.segments[0].name == "INS" else None

    @property
    def is_subscriber(self) -> bool:
        """INS01 Y means the loop is the subscriber, N means a dependent."""
        ins = self.ins
        return bool(ins) and ins.get(1).strip().upper() == "Y"

    @property
    def applies_to(self) -> str:
        return "SUB" if self.is_subscriber else "DEP"

    def by_name(self, name: str) -> List:
        return [segment for segment in self.segments if segment.name == name]

    def as_element_records(self) -> List[dict]:
        """
        Flat element view with occurrence counted *within this loop*, which is
        what a mapping rule means when it says "the second REF".
        """
        records: List[dict] = []
        counts: dict = {}
        for segment in self.segments:
            counts[segment.name] = counts.get(segment.name, 0) + 1
            records.extend(segment.as_element_records(occurrence=counts[segment.name]))
        return records

    # Kept so any caller still expecting the old dict shape keeps working.
    def as_dict(self) -> dict:
        return {"loop_id": self.loop_id, "data": self.as_element_records()}


@dataclass
class ParsedFile:
    header: List = field(default_factory=list)
    loops: List[MemberLoop] = field(default_factory=list)

    def __iter__(self):
        return iter(self.loops)

    def __len__(self):
        return len(self.loops)


def iter_member_loops(segments: Iterable) -> Iterator[MemberLoop]:
    """
    Stream member loops. Header segments are skipped, so pair this with
    extract_loops() when the header is needed too.
    """
    current: List = []
    loop_id = 0
    for segment in segments:
        if segment.name == "INS":
            if current:
                loop_id += 1
                yield MemberLoop(loop_id=loop_id, segments=current)
            current = [segment]
        elif segment.name in ("SE", "GE", "IEA"):
            # Trailers belong to no member; flush whatever is open and stop
            # accumulating so the last loop does not swallow them.
            if current:
                loop_id += 1
                yield MemberLoop(loop_id=loop_id, segments=current)
                current = []
        elif current:
            current.append(segment)
        # Segments before the first INS are header and are dropped here.
    if current:
        loop_id += 1
        yield MemberLoop(loop_id=loop_id, segments=current)


def extract_loops(segments: Iterable) -> ParsedFile:
    """
    Materialise header plus member loops.

    Accepts an iterable of Segment objects. If handed the legacy flat element
    dicts it raises rather than silently producing the old broken output.
    """
    segments = list(segments)
    if segments and isinstance(segments[0], dict):
        raise TypeError(
            "extract_loops() now takes Segment objects from EDI834Parser.iter_segments(). "
            "Passing flat element dicts loses the segment grouping that loop detection needs."
        )

    header: List = []
    for segment in segments:
        if segment.name == "INS":
            break
        header.append(segment)

    return ParsedFile(header=header, loops=list(iter_member_loops(segments)))


def attach_dependents(loops: List[MemberLoop]) -> List[dict]:
    """
    Group each subscriber with the dependent loops that follow it.

    In an 834 the dependents of a subscriber are the INS*N loops between that
    subscriber's INS*Y and the next INS*Y. Useful for the flat Excel layout,
    where a row can carry both the subscriber and dependent name columns.
    """
    families: List[dict] = []
    current = None
    for loop in loops:
        if loop.is_subscriber or current is None:
            current = {"subscriber": loop, "dependents": []}
            families.append(current)
        else:
            current["dependents"].append(loop)
    return families


class StreamingParsedFile:
    """
    Single-pass view over a segment stream.

    extract_loops() materialises everything, which is fine for a few thousand
    members and wasteful for a hundred thousand. This class captures the header
    as it goes past and then yields member loops one at a time, so peak memory
    is one loop rather than the whole file. Counters are populated as a side
    effect and are only complete once iteration has finished.
    """

    def __init__(self, segments: Iterable):
        self._segments = iter(segments)
        self.header: List = []
        self.loop_count = 0
        self.subscriber_count = 0

    def __iter__(self) -> Iterator[MemberLoop]:
        current: List = []
        for segment in self._segments:
            if segment.name == "INS":
                if current:
                    yield self._emit(current)
                current = [segment]
            elif segment.name in ("SE", "GE", "IEA"):
                if current:
                    yield self._emit(current)
                    current = []
            elif current:
                current.append(segment)
            else:
                self.header.append(segment)
        if current:
            yield self._emit(current)

    def _emit(self, segments: List) -> MemberLoop:
        self.loop_count += 1
        loop = MemberLoop(loop_id=self.loop_count, segments=segments)
        if loop.is_subscriber:
            self.subscriber_count += 1
        return loop

    @property
    def dependent_count(self) -> int:
        return self.loop_count - self.subscriber_count

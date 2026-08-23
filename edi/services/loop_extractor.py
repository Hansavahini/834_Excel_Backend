"""
Split a segment stream into the header and one loop per INS.
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
        return (
            self.segments[0]
            if self.segments and self.segments[0].name == "INS"
            else None
        )

    @property
    def is_subscriber(self) -> bool:
        """INS01 Y means the loop is the subscriber, N means a dependent."""
        ins = self.ins
        return bool(ins) and ins.get(1).strip().upper() == "Y"

    @property
    def applies_to(self) -> str:
        return "SUB" if self.is_subscriber else "DEP"

    def by_name(self, name: str) -> List:
        return [
            segment
            for segment in self.segments
            if segment.name == name
        ]

    def as_element_records(self) -> List[dict]:
        """
        Flat element view with occurrence counted within this loop.
        """
        records: List[dict] = []
        counts: dict = {}

        for segment in self.segments:
            counts[segment.name] = counts.get(segment.name, 0) + 1

            records.extend(
                segment.as_element_records(
                    occurrence=counts[segment.name]
                )
            )

        return records

    def as_dict(self) -> dict:
        return {
            "loop_id": self.loop_id,
            "data": self.as_element_records(),
        }


@dataclass
class ParsedFile:
    header: List = field(default_factory=list)
    loops: List[MemberLoop] = field(default_factory=list)

    def __iter__(self):
        return iter(self.loops)

    def __len__(self):
        return len(self.loops)


def iter_member_loops(
    segments: Iterable,
) -> Iterator[MemberLoop]:
    """
    Stream member loops.

    Each INS segment starts a new member loop.
    Segments before the first INS are treated as header data.
    """
    current: List = []
    loop_id = 0

    for segment in segments:
        if segment.name == "INS":
            if current:
                loop_id += 1
                yield MemberLoop(
                    loop_id=loop_id,
                    segments=current,
                )

            current = [segment]

        elif segment.name in ("SE", "GE", "IEA"):
            if current:
                loop_id += 1
                yield MemberLoop(
                    loop_id=loop_id,
                    segments=current,
                )
                current = []

        elif current:
            current.append(segment)

    if current:
        loop_id += 1
        yield MemberLoop(
            loop_id=loop_id,
            segments=current,
        )


def extract_loops(segments: Iterable) -> ParsedFile:
    """
    Materialise header plus member loops.

    Accepts an iterable of Segment objects. If handed the legacy flat
    element dicts it raises rather than silently producing the old
    broken output.
    """
    segments = list(segments)

    if segments and isinstance(segments[0], dict):
        raise TypeError(
            "extract_loops() now takes Segment objects from "
            "EDI834Parser.iter_segments(). Passing flat element dicts "
            "loses the segment grouping that loop detection needs."
        )

    header: List = []

    for segment in segments:
        if segment.name == "INS":
            break

        header.append(segment)

    return ParsedFile(
        header=header,
        loops=list(iter_member_loops(segments)),
    )


class StreamingParsedFile:
    """
    Single-pass view over a segment stream.

    Captures header segments as they pass and yields one MemberLoop
    at a time, keeping memory usage low for large EDI files.
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
                # Segments before the first INS are file-level header data.
                self.header.append(segment)

        if current:
            yield self._emit(current)

    def _emit(self, segments: List) -> MemberLoop:
        self.loop_count += 1

        loop = MemberLoop(
            loop_id=self.loop_count,
            segments=segments,
        )

        if loop.is_subscriber:
            self.subscriber_count += 1

        return loop

    @property
    def dependent_count(self) -> int:
        return self.loop_count - self.subscriber_count
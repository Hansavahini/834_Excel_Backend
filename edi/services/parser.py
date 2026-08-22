"""
X12 834 parser.

Design note, because this is the change that matters most. The previous parser
flattened the file straight to a list of element records:

    {"segment": "NM1", "element": "NM103", "value": "MERCER", "occurrence": 7}

That shape throws away the one fact the whole downstream pipeline depends on:
which NM1 instance a given NM103 came from. An 834 carries NM1 for the insured
(NM101=IL), the sponsor (P5), the payer (IN) and the custodial parent (S3), and
DTP03 is a begin date, an end date or a hire date depending entirely on DTP01.
Once the elements are flat you cannot answer "the NM103 whose NM101 is IL", so
qualifier-based mapping is impossible and INS loop boundaries are unfindable.

So the parser now yields Segment objects that keep their elements together and
know their ordinal position in the file. Flattening, when a caller wants it,
happens afterwards and per loop.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

logger = logging.getLogger("edi.parser")

DEFAULT_ELEMENT_SEP = "*"
DEFAULT_COMPONENT_SEP = ":"
DEFAULT_SEGMENT_SEP = "~"
DEFAULT_REPETITION_SEP = "^"

# ISA is fixed-width by specification: 105 characters followed by the segment
# terminator. The separators live at known offsets inside that window.
ISA_LENGTH = 105


class EDIParseError(ValueError):
    """Raised when the byte stream cannot be read as X12 at all."""


@dataclass
class Delimiters:
    element: str = DEFAULT_ELEMENT_SEP
    component: str = DEFAULT_COMPONENT_SEP
    segment: str = DEFAULT_SEGMENT_SEP
    repetition: str = DEFAULT_REPETITION_SEP

    @classmethod
    def sniff(cls, head: str) -> "Delimiters":
        """
        Read the delimiters out of the ISA header rather than assuming them.

        Every trading partner is entitled to its own separators and several do
        use them. Hard-coding '*' and '~' works until the first partner sends
        '|' and a newline, at which point the file parses as a single segment
        and the converter silently produces an empty workbook.
        """
        if not head.startswith("ISA"):
            raise EDIParseError(
                "File does not begin with an ISA segment; this is not an X12 interchange."
            )
        if len(head) < ISA_LENGTH + 1:
            raise EDIParseError("ISA header is truncated; the file is incomplete or corrupt.")

        element = head[3]
        component = head[ISA_LENGTH - 1]
        segment = head[ISA_LENGTH]
        # ISA11 is the repetition separator in 005010. Older 004010 files put a
        # usage indicator there instead, so only take it when it looks like one.
        fields = head[:ISA_LENGTH].split(element)
        repetition = DEFAULT_REPETITION_SEP
        if len(fields) > 11 and len(fields[11]) == 1 and not fields[11].isalnum():
            repetition = fields[11]
        return cls(element=element, component=component, segment=segment, repetition=repetition)


@dataclass
class Segment:
    """One X12 segment with its elements kept together."""

    name: str
    elements: List[str]  # elements[0] is element 01, so NM103 is elements[2]
    position: int  # 1-based ordinal within the file, for error messages
    delimiters: Delimiters = field(default_factory=Delimiters, repr=False)

    def get(self, index: int, default: str = "") -> str:
        """1-based element accessor, so seg.get(3) is NM103."""
        if 1 <= index <= len(self.elements):
            return self.elements[index - 1]
        return default

    def component(self, index: int, sub_index: int, default: str = "") -> str:
        """1-based sub-element accessor for composite elements such as HD03."""
        raw = self.get(index)
        if not raw:
            return default
        parts = raw.split(self.delimiters.component)
        if 1 <= sub_index <= len(parts):
            return parts[sub_index - 1]
        return default

    def element_code(self, index: int) -> str:
        return "{name}{index:02d}".format(name=self.name, index=index)

    def as_element_records(self, occurrence: int = 1) -> List[dict]:
        """
        Flat view, retained so the mapping-builder UI keeps working. The
        occurrence passed in should be the occurrence within its loop, which is
        the only counter a mapping rule can meaningfully refer to.
        """
        return [
            {
                "segment": self.name,
                "element": self.element_code(index),
                "value": value,
                "occurrence": occurrence,
                "position": self.position,
            }
            for index, value in enumerate(self.elements, start=1)
        ]


class EDI834Parser:
    """
    Streams an 834 into Segment objects.

    The file is read in chunks and segments are emitted as they complete, so a
    200 MB interchange costs a few megabytes of resident memory instead of
    being materialised twice, once as a string and again as a million dicts.
    """

    def __init__(self, file_path: str, chunk_size: int = 1024 * 1024):
        self.file_path = file_path
        self.chunk_size = chunk_size
        self.delimiters: Optional[Delimiters] = None

    def _open(self) -> io.TextIOBase:
        # utf-8-sig strips the BOM Windows tooling adds; errors="replace" keeps
        # one stray high byte in a member name from failing an entire file.
        return open(self.file_path, "r", encoding="utf-8-sig", errors="replace", newline="")

    def read_file(self) -> str:
        """Whole-file read, kept for small files and tests. Prefer iter_segments()."""
        with self._open() as handle:
            return handle.read()

    def iter_segments(self) -> Iterator[Segment]:
        """Yield Segment objects one at a time without holding the file in memory."""
        with self._open() as handle:
            head = handle.read(ISA_LENGTH + 1)
            if not head.strip():
                raise EDIParseError("File is empty.")
            self.delimiters = Delimiters.sniff(head)
            delims = self.delimiters

            buffer = head
            position = 0
            while True:
                chunk = handle.read(self.chunk_size)
                if chunk:
                    buffer += chunk
                pieces = buffer.split(delims.segment)
                # The last piece may be a partial segment, so hold it back
                # unless we have reached end of file.
                buffer = pieces.pop() if chunk else ""
                for raw in pieces:
                    segment = self._build(raw, position + 1, delims)
                    if segment is not None:
                        position += 1
                        yield segment
                if not chunk:
                    if buffer.strip():
                        segment = self._build(buffer, position + 1, delims)
                        if segment is not None:
                            yield segment
                    break

    @staticmethod
    def _build(raw: str, position: int, delims: Delimiters) -> Optional[Segment]:
        # Line breaks are cosmetic in X12 and turn up in files a human has
        # opened and re-saved. Strip them, never treat them as data.
        raw = raw.strip().strip("\r\n")
        if not raw:
            return None
        elements = raw.split(delims.element)
        name = elements[0].strip().upper()
        if not name:
            return None
        return Segment(name=name, elements=elements[1:], position=position, delimiters=delims)

    def parse(self) -> List[Segment]:
        """Materialise every segment. Fine for a few thousand, use iter_segments() above that."""
        return list(self.iter_segments())

    def extract_elements(self, content: Optional[str] = None) -> List[dict]:
        """
        Backwards-compatible flat element list, kept because the mapping-builder
        screen needs to show every segment and element found in a sample file.
        The occurrence counter here is global to the file and is only meaningful
        for header segments; loop-scoped occurrence comes from loop_extractor.
        """
        records: List[dict] = []
        counts: dict = {}

        if content is None:
            segments: Iterator[Segment] = self.iter_segments()
        else:
            delims = (
                Delimiters.sniff(content[: ISA_LENGTH + 1])
                if content.startswith("ISA")
                else Delimiters()
            )
            self.delimiters = delims
            segments = (
                seg
                for seg in (
                    self._build(raw, index + 1, delims)
                    for index, raw in enumerate(content.split(delims.segment))
                )
                if seg is not None
            )

        for segment in segments:
            counts[segment.name] = counts.get(segment.name, 0) + 1
            records.extend(segment.as_element_records(occurrence=counts[segment.name]))
        return records


def envelope_facts(segments) -> dict:
    """
    Pull the header facts the UploadedFile row wants: control numbers, trading
    partner ids, sponsor name and the business date the file represents.
    """
    facts = {
        "interchange_control_number": "",
        "group_control_number": "",
        "transaction_set_control_number": "",
        "sender_id": "",
        "receiver_id": "",
        "sponsor_name": "",
        "file_date": "",
    }
    for segment in segments:
        if segment.name == "ISA":
            facts["sender_id"] = segment.get(6).strip()
            facts["receiver_id"] = segment.get(8).strip()
            facts["interchange_control_number"] = segment.get(13).strip()
        elif segment.name == "GS":
            facts["group_control_number"] = segment.get(6).strip()
            facts["file_date"] = facts["file_date"] or segment.get(4).strip()
        elif segment.name == "ST":
            facts["transaction_set_control_number"] = segment.get(2).strip()
        elif segment.name == "BGN":
            # BGN03 is the transaction creation date and is the better answer
            # when present, so it overwrites GS04.
            if segment.get(3).strip():
                facts["file_date"] = segment.get(3).strip()
        elif segment.name == "N1" and segment.get(1).strip() == "P5":
            facts["sponsor_name"] = segment.get(2).strip()[:60]
        elif segment.name == "INS":
            break  # the header is over
    return facts

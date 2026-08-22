"""
Workbook writer.

Three problems with the original. It saved to a bare relative filename, so the
workbook landed in whatever directory the process happened to start in, which
under gunicorn is not a directory anyone can find. The filename was the constant
"converted_834.xlsx", so two users converting at the same time overwrote each
other and the second one silently got the first one's PHI. And it built the
whole sheet in memory, which on a 27,000 member file is a large object graph
held for the duration of a request.

This version writes into MEDIA_ROOT/generated under a unique name, uses
openpyxl's write-only mode so rows are streamed to the zip as they are produced,
and returns enough metadata to populate a GeneratedFile row.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

from django.conf import settings
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

logger = logging.getLogger("edi.excel")

MAX_SHEET_ROWS = 1_048_576  # Excel's hard limit, worth failing loudly on


@dataclass
class GeneratedWorkbook:
    absolute_path: str
    relative_path: str
    filename: str
    row_count: int
    size_bytes: int


def _output_dir(owner_id=None) -> str:
    parts = [str(settings.MEDIA_ROOT), getattr(settings, "GENERATED_SUBDIR", "generated")]
    if owner_id:
        parts.append(str(owner_id))
    parts.append(datetime.now().strftime("%Y/%m/%d"))
    directory = os.path.join(*parts)
    os.makedirs(directory, exist_ok=True)
    return directory


def build_filename(source_name: str = "834") -> str:
    """Unique per run, and still recognisable to whoever downloads it."""
    stem = os.path.splitext(os.path.basename(source_name or "834"))[0][:40]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return "{stem}_{stamp}_{token}.xlsx".format(stem=stem, stamp=stamp, token=uuid.uuid4().hex[:8])


def generate_excel(
    headers: List[str],
    rows: Iterable[dict],
    output_path: Optional[str] = None,
    *,
    owner_id=None,
    source_name: str = "834",
    sheet_title: str = "834 Conversion",
) -> GeneratedWorkbook:
    """
    Write the workbook and return where it went.

    output_path is still honoured when a caller passes one, so existing tests
    keep working, but it is now resolved against MEDIA_ROOT when relative rather
    than against the process working directory.
    """
    if not headers:
        raise ValueError("Cannot generate a workbook with no columns; check the mapping template.")

    if output_path:
        if not os.path.isabs(output_path):
            output_path = os.path.join(_output_dir(owner_id), os.path.basename(output_path))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        absolute_path = output_path
    else:
        absolute_path = os.path.join(_output_dir(owner_id), build_filename(source_name))

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_title[:31])

    bold = Font(bold=True)
    header_cells = []
    for text in headers:
        from openpyxl.cell import WriteOnlyCell

        cell = WriteOnlyCell(sheet, value=text)
        cell.font = bold
        header_cells.append(cell)
    sheet.append(header_cells)

    # Openpyxl's write-only sheets cannot set column widths after the fact, so
    # approximate from the header text; it is the difference between a readable
    # workbook and forty columns of '#####'.
    sheet.column_dimensions  # touch so the attribute exists
    for index, text in enumerate(headers, start=1):
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = min(max(len(str(text)) + 4, 12), 40)

    row_count = 0
    for row_data in rows:
        row_data.pop("__warnings__", None)
        sheet.append([row_data.get(header, "") for header in headers])
        row_count += 1
        if row_count >= MAX_SHEET_ROWS - 1:
            raise ValueError(
                "This file exceeds the {limit:,} row Excel sheet limit. "
                "Split the conversion or export CSV instead.".format(limit=MAX_SHEET_ROWS)
            )

    workbook.save(absolute_path)
    workbook.close()

    size_bytes = os.path.getsize(absolute_path)
    relative_path = os.path.relpath(absolute_path, str(settings.MEDIA_ROOT))
    logger.info(
        "wrote workbook %s rows=%d bytes=%d", os.path.basename(absolute_path), row_count, size_bytes
    )

    return GeneratedWorkbook(
        absolute_path=absolute_path,
        relative_path=relative_path.replace(os.sep, "/"),
        filename=os.path.basename(absolute_path),
        row_count=row_count,
        size_bytes=size_bytes,
    )

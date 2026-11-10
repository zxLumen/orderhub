"""Minimal XLSX reader (standard library only).

Platform "personal data copies" are often .xlsx. Pulling in openpyxl would
break the zero-dependency promise of OrderHub, so this module understands just
enough of the format: shared strings, inline strings, numbers and the first
worksheet. Formulas are ignored (their cached value is used when present).
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any

_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(ref: str) -> int:
    letters = _CELL_REF.match(ref)
    if not letters:
        return 0
    value = 0
    for char in letters.group(1):
        value = value * 26 + (ord(char) - 64)
    return value - 1


def _text_of(node: Any) -> str:
    # <si>/<is> may contain several <r><t> runs.
    parts = [child.text or "" for child in node.iter() if _local(child.tag) == "t"]
    return "".join(parts)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        info = archive.getinfo("xl/sharedStrings.xml")
        if info.file_size > 30 * 1024 * 1024:
            return []
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    return [_text_of(node) for node in root if _local(node.tag) == "si"]


def _sheets(archive: zipfile.ZipFile) -> list[str]:
    names = [
        name
        for name in archive.namelist()
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    ]
    return sorted(names)


def read_xlsx(data: bytes, *, max_rows: int = 5000) -> list[list[str]]:
    """Return rows of strings from the first non-empty worksheet."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(BytesIO(data)) as archive:
        shared = _shared_strings(archive)
        best: list[list[str]] = []
        for name in _sheets(archive):
            info = archive.getinfo(name)
            if info.file_size > 30 * 1024 * 1024:
                continue
            root = ET.fromstring(archive.read(name))
            rows: list[list[str]] = []
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                values: dict[int, str] = {}
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    ref = cell.attrib.get("r") or ""
                    index = _column_index(ref)
                    cell_type = cell.attrib.get("t") or ""
                    value = ""
                    if cell_type == "inlineStr":
                        inline = next(
                            (child for child in cell if _local(child.tag) == "is"), None
                        )
                        value = _text_of(inline) if inline is not None else ""
                    else:
                        raw_value = next(
                            (child for child in cell if _local(child.tag) == "v"), None
                        )
                        value = raw_value.text if raw_value is not None and raw_value.text else ""
                        if cell_type == "s" and value.isdigit():
                            idx = int(value)
                            value = shared[idx] if 0 <= idx < len(shared) else ""
                    values[index] = (value or "").strip()
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
                if len(rows) >= max_rows:
                    break
            if len(rows) > len(best):
                best = rows
        return best

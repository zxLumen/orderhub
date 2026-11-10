"""File helpers for bill / order-export import.

Supports the formats a normal user can actually download from their phone:

* Alipay bill ZIP/CSV (GBK, several preamble lines, header in the middle)
* WeChat Pay bill CSV (UTF-8 BOM, ~16 preamble lines)
* platform "personal data copy" CSV / XLSX / JSON / ZIP
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

HEADER_TOKENS = {
    "交易时间", "交易类型", "交易分类", "交易对方", "商品说明", "商品名称", "商品",
    "收/支", "金额", "金额(元)", "交易状态", "当前状态", "交易订单号", "交易单号",
    "商家订单号", "商户单号", "订单号", "订单编号", "订单状态", "实付款",
    "实付金额", "下单时间", "创建时间", "运单号", "快递单号", "店铺名称", "店铺",
    "快递公司", "商品标题", "宝贝标题", "收/付款方式", "支付方式", "备注",
}

DATA_EXTENSIONS = {".csv", ".txt", ".tsv", ".json", ".xlsx", ".xls"}


@dataclass
class Table:
    name: str
    headers: list[str]
    rows: list[dict[str, str]]
    raw_rows: list[list[str]] = field(default_factory=list)


def decode_bytes(data: bytes) -> str:
    """Decode Chinese CSV exports without pulling in chardet."""
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Heuristic: GBK decoding of UTF-8 bytes produces many rare CJK chars,
        # but the replacement-free UTF-8 path is checked first.
        return text
    return data.decode("utf-8", "replace")


def normalize_header(value: str) -> str:
    text = (value or "").replace("\ufeff", "").strip()
    text = text.replace("（", "(").replace("）", ")")
    return "".join(text.split())


def _parse_csv_line(line: str) -> list[str]:
    try:
        return next(csv.reader([line]))
    except (StopIteration, csv.Error):
        return []


def _score_header(cells: list[str]) -> int:
    score = 0
    for cell in cells:
        if normalize_header(cell) in HEADER_TOKENS:
            score += 1
    return score


def find_header_row(lines: list[str], *, limit: int = 60) -> int:
    best_index = -1
    best_score = 0
    for index, line in enumerate(lines[:limit]):
        if not line.strip():
            continue
        cells = _parse_csv_line(line)
        if len(cells) < 2:
            continue
        score = _score_header(cells)
        if score > best_score:
            best_score = score
            best_index = index
    return best_index if best_score >= 2 else -1


def parse_csv_text(text: str, *, name: str = "csv") -> Table | None:
    lines = text.splitlines()
    index = find_header_row(lines)
    if index < 0:
        return None
    reader = csv.reader(io.StringIO("\n".join(lines[index:])))
    try:
        headers = [normalize_header(cell) for cell in next(reader)]
    except StopIteration:
        return None
    rows: list[dict[str, str]] = []
    raw_rows: list[list[str]] = []
    for cells in reader:
        if not any((cell or "").strip() for cell in cells):
            continue
        raw_rows.append(cells)
        row: dict[str, str] = {}
        for key, value in zip(headers, cells):
            if key:
                row[key] = (value or "").strip()
        rows.append(row)
    if not rows:
        return None
    return Table(name=name, headers=headers, rows=rows, raw_rows=raw_rows)


def parse_json_text(text: str, *, name: str = "json") -> Table | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, dict):
        for key in ("orders", "data", "list", "items", "records", "订单"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            payload = [payload]
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    if not isinstance(first, dict):
        return None

    headers: list[str] = []
    for item in payload[:50]:
        if not isinstance(item, dict):
            continue
        for key in item:
            key_text = normalize_header(str(key))
            if key_text and key_text not in headers:
                headers.append(key_text)
    rows: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        row: dict[str, str] = {}
        for key, value in item.items():
            key_text = normalize_header(str(key))
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            row[key_text] = "" if value is None else str(value).strip()
        rows.append(row)
    if not rows:
        return None
    return Table(name=name, headers=headers, rows=rows)


def _table_from_bytes(data: bytes, name: str) -> Table | None:
    lowered = name.lower()
    if lowered.endswith((".xlsx", ".xls")):
        from .xlsx import read_xlsx

        try:
            grid = read_xlsx(data)
        except (zipfile.BadZipFile, KeyError, ValueError):
            return None
        if not grid:
            return None
        # Find the header row inside the sheet (exports often start with a
        # title row or account info before the actual columns).
        index = 0
        best_score = 0
        for pos, row in enumerate(grid[:60]):
            score = _score_header([str(cell) for cell in row])
            if score > best_score:
                best_score = score
                index = pos
        if best_score < 2:
            index = 0
        headers = [normalize_header(cell) for cell in grid[index]]
        rows = []
        for cells in grid[index + 1 :]:
            if not any((cell or "").strip() for cell in cells):
                continue
            row = {key: (cells[pos] if pos < len(cells) else "").strip()
                   for pos, key in enumerate(headers) if key}
            rows.append(row)
        if not rows:
            return None
        return Table(name=name, headers=headers, rows=rows, raw_rows=grid[index + 1 :])

    text = decode_bytes(data)
    if lowered.endswith(".json") or text.lstrip().startswith(("[", "{")):
        table = parse_json_text(text, name=name)
        if table:
            return table
    return parse_csv_text(text, name=name)


def load_tables(data: bytes, filename: str = "") -> tuple[list[Table], str | None]:
    """Return tables plus a human readable detect error, if any."""
    name = (filename or "upload").strip()
    if name.lower().endswith((".xlsx", ".xls")):
        table = _table_from_bytes(data, name)
        if table:
            return [table], None
        return [], "Excel 里没找到表头（需要含 订单号/交易时间/商品 等列）"
    if data[:2] == b"PK":
        tables: list[Table] = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total_uncompressed = 0
                for entry in archive.namelist():
                    base = entry.rsplit("/", 1)[-1]
                    if not base or base.startswith("."):
                        continue
                    suffix = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
                    if suffix not in DATA_EXTENSIONS:
                        continue
                    try:
                        info = archive.getinfo(entry)
                    except KeyError:
                        continue
                    # Refuse zip bombs: one file over 20MB or 50MB in total.
                    if info.file_size > 20 * 1024 * 1024:
                        continue
                    total_uncompressed += info.file_size
                    if total_uncompressed > 50 * 1024 * 1024:
                        break
                    try:
                        inner = archive.read(entry)
                    except (KeyError, RuntimeError):
                        continue
                    table = _table_from_bytes(inner, base)
                    if table:
                        tables.append(table)
        except zipfile.BadZipFile:
            return [], "压缩包损坏"
        if not tables:
            return [], "压缩包里没找到可识别的 CSV/XLSX/JSON"
        return tables, None

    table = _table_from_bytes(data, name)
    if not table:
        return [], "没找到表头（需要含 订单号/交易时间/商品 等列）"
    return [table], None

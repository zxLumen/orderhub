"""SQLite storage and full-text search.

Chinese text is indexed with character bigrams so search works without an
external tokenizer or a search service. The database is intentionally small and
can be copied/exported as a single file.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config
from .services.tracking import extract as extract_tracking

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
ASCII_WORD = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,}")

# Chinese platforms report local time without an offset. Store UTC internally,
# but interpret naive timestamps as China Standard Time. Fixed offset avoids a
# dependency on the IANA tz database (not bundled with every Windows Python).
CHINA_TZ = timezone(timedelta(hours=8))

# Shipment statuses are logistics states. Refunds are order-level and must not
# overwrite a parcel's delivery state.
SHIPMENT_STATUSES = {
    "unknown",
    "accepted",
    "in_transit",
    "out_for_delivery",
    "ready_for_pickup",
    "delivering",
    "delivered",
    "returned",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_time(value: str | None) -> str | None:
    """Best-effort ISO timestamp; keeps unknown strings intact."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def tokenize(text: str) -> list[str]:
    """Character bigrams + ASCII words; good enough for Chinese product names."""
    if not text:
        return []
    text = text.lower()
    tokens: list[str] = []

    for word in ASCII_WORD.findall(text):
        tokens.append(word)

    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        # Unigrams help with single-character queries; bigrams carry meaning.
        tokens.extend(list(run))
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    # Stable unique order
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def tokenize_query(text: str) -> list[str]:
    """Use bigrams (not single characters) for multi-character Chinese queries.

    Indexing keeps unigrams for one-character searches, but querying with them
    makes "充电线" match unrelated words such as "无线". Bigrams avoid that.
    """
    if not text:
        return []
    text = text.lower()
    tokens: list[str] = list(ASCII_WORD.findall(text))
    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_order_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    shop TEXT NOT NULL DEFAULT '',
    amount REAL,
    status TEXT NOT NULL DEFAULT 'unknown',
    status_text TEXT NOT NULL DEFAULT '',
    order_time TEXT,
    tracking_no TEXT,
    url TEXT,
    product_url TEXT,
    image_url TEXT,
    logistics_text TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_order_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(order_time DESC);
CREATE INDEX IF NOT EXISTS idx_orders_platform ON orders(platform);
CREATE INDEX IF NOT EXISTS idx_orders_tracking ON orders(tracking_no);

CREATE TABLE IF NOT EXISTS shipments (
    tracking_no TEXT PRIMARY KEY,
    carrier TEXT NOT NULL DEFAULT '',
    carrier_name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    status_text TEXT NOT NULL DEFAULT '',
    pickup_code TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    last_event_at TEXT,
    order_id INTEGER,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_shipments_updated ON shipments(updated_at DESC);

CREATE TABLE IF NOT EXISTS order_shipments (
    order_id INTEGER NOT NULL,
    tracking_no TEXT NOT NULL,
    package_index INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_id, tracking_no)
);
CREATE INDEX IF NOT EXISTS idx_order_shipments_tracking ON order_shipments(tracking_no);

CREATE TABLE IF NOT EXISTS parcel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_no TEXT NOT NULL,
    event_time TEXT,
    context TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(tracking_no, event_time, context)
);
CREATE INDEX IF NOT EXISTS idx_parcel_events_no ON parcel_events(tracking_no, event_time);

CREATE TABLE IF NOT EXISTS sms_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    raw_text TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    platform TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    tracking_no TEXT NOT NULL DEFAULT '',
    carrier TEXT NOT NULL DEFAULT '',
    pickup_code TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    ignored INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_received ON sms_events(received_at DESC);

CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL DEFAULT 'douyin',
    text TEXT NOT NULL,
    image_path TEXT NOT NULL DEFAULT '',
    captured_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captures_time ON captures(captured_at DESC);

CREATE TABLE IF NOT EXISTS collector_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    items INTEGER NOT NULL DEFAULT 0,
    captcha INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_platform ON collector_runs(platform, started_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);

CREATE TABLE IF NOT EXISTS collector_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'recent',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT,
    worker TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON collector_commands(status, id);

CREATE TABLE IF NOT EXISTS collector_workers (
    name TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    chrome_ok INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_docs (
    tokens TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    UNIQUE(doc_type, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_search_docs_type ON search_docs(doc_type, doc_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    tokens,
    doc_type UNINDEXED,
    doc_id UNINDEXED,
    title UNINDEXED,
    platform UNINDEXED,
    occurred_at UNINDEXED,
    tokenize='unicode61'
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Connection that is always closed, with commit/rollback semantics."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    if db_path is None:
        db_file = config.DB_PATH
        debug_dir = config.DEBUG_DIR
    else:
        db_file = Path(db_path)
        if not db_file.is_absolute():
            db_file = Path.cwd() / db_file
        debug_dir = db_file.parent / "debug"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    with session(db_file) as conn:
        conn.executescript(SCHEMA)
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            # Some minimal SQLite builds omit FTS5; search falls back to LIKE.
            pass
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release of the schema."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    for name in ("image_url", "product_url", "logistics_text"):
        if name not in columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT")
    shipment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(shipments)")}
    if "raw_json" not in shipment_columns:
        conn.execute("ALTER TABLE shipments ADD COLUMN raw_json TEXT")


def _fts_delete(conn: sqlite3.Connection, doc_type: str, doc_id: str) -> None:
    conn.execute(
        "DELETE FROM search_docs WHERE doc_type = ? AND doc_id = ?",
        (doc_type, str(doc_id)),
    )
    try:
        conn.execute(
            "DELETE FROM search_fts WHERE doc_type = ? AND doc_id = ?",
            (doc_type, str(doc_id)),
        )
    except sqlite3.OperationalError:
        pass


def _fts_insert(
    conn: sqlite3.Connection,
    *,
    doc_type: str,
    doc_id: str,
    title: str,
    body: str,
    platform: str,
    occurred_at: str | None,
) -> None:
    _fts_delete(conn, doc_type, doc_id)
    tokens = tokenize(" ".join([title or "", body or "", platform or ""]))
    if not tokens:
        return
    token_text = " ".join(tokens)
    conn.execute(
        "INSERT OR REPLACE INTO search_docs(tokens, doc_type, doc_id, title, platform, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token_text, doc_type, str(doc_id), title or "", platform or "", occurred_at or ""),
    )
    try:
        conn.execute(
            "INSERT INTO search_fts(tokens, doc_type, doc_id, title, platform, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_text, doc_type, str(doc_id), title or "", platform or "", occurred_at or ""),
        )
    except sqlite3.OperationalError:
        pass


def _synthetic_order_id(
    platform: str,
    title: str,
    amount: float | None,
    order_time: str | None,
    tracking_no: str | None,
) -> str:
    raw = f"{platform}|{title}|{amount}|{order_time}|{tracking_no or ''}"
    return "local-" + sha256(raw)[:20]


def ensure_shipment(
    conn: sqlite3.Connection,
    *,
    tracking_no: str,
    carrier: str = "",
    carrier_name: str = "",
    platform: str = "",
    order_id: int | None = None,
) -> None:
    """Create a placeholder without an event timestamp.

    A later real tracking event must not be rejected as "stale" just because the
    order row was created first with an unknown status.
    """
    tracking_no = (tracking_no or "").strip().upper()
    if not tracking_no:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO shipments(tracking_no, carrier, carrier_name, platform,
                                        status, status_text, pickup_code, location,
                                        last_event_at, order_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'unknown', '', '', '', NULL, ?, ?, ?)
        """,
        (tracking_no, carrier or "", carrier_name or "", (platform or "").lower(), order_id, now, now),
    )


def link_order_shipment(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    tracking_no: str,
    package_index: int = 0,
    source: str = "",
) -> None:
    tracking_no = (tracking_no or "").strip().upper()
    if not tracking_no:
        return
    conn.execute(
        """
        INSERT INTO order_shipments(order_id, tracking_no, package_index, source, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(order_id, tracking_no) DO UPDATE SET
            package_index=excluded.package_index,
            source=COALESCE(NULLIF(excluded.source, ''), order_shipments.source)
        """,
        (order_id, tracking_no, int(package_index or 0), source or "", utc_now()),
    )


def list_order_shipments(conn: sqlite3.Connection, order_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT os.package_index, os.source, s.*
            FROM order_shipments os
            JOIN shipments s ON s.tracking_no = os.tracking_no
            WHERE os.order_id=?
            ORDER BY os.package_index, os.created_at
            """,
            (order_id,),
        ).fetchall()
    ]


def get_order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result.pop("raw_json", None)
    result["shipments"] = []
    for shipment in list_order_shipments(conn, order_id):
        shipment["events"] = [
            dict(event)
            for event in conn.execute(
                "SELECT * FROM parcel_events WHERE tracking_no=? "
                "ORDER BY COALESCE(event_time, created_at) DESC LIMIT 100",
                (shipment["tracking_no"],),
            ).fetchall()
        ]
        result["shipments"].append(shipment)
    return result


def upsert_order(
    conn: sqlite3.Connection,
    *,
    platform: str,
    platform_order_id: str | None = None,
    title: str = "",
    shop: str = "",
    amount: float | None = None,
    status: str = "unknown",
    status_text: str = "",
    order_time: str | None = None,
    tracking_no: str | None = None,
    url: str | None = None,
    product_url: str | None = None,
    image_url: str | None = None,
    logistics_text: str | None = None,
    raw: Any = None,
) -> int:
    platform = (platform or "unknown").strip().lower()
    order_time = normalize_time(order_time)
    tracking_no = tracking_no.strip().upper() if tracking_no else None
    image_url = (image_url or "").strip() or None
    product_url = (product_url or "").strip() or None
    logistics_text = (logistics_text or "").strip() or None
    platform_order_id = (platform_order_id or "").strip() or _synthetic_order_id(
        platform, title, amount, order_time, tracking_no
    )
    now = utc_now()
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
    conn.execute(
        """
        INSERT INTO orders(platform, platform_order_id, title, shop, amount, status,
                           status_text, order_time, tracking_no, url, product_url,
                           image_url, logistics_text, raw_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, platform_order_id) DO UPDATE SET
            title=COALESCE(NULLIF(excluded.title, ''), orders.title),
            shop=COALESCE(NULLIF(excluded.shop, ''), orders.shop),
            amount=COALESCE(excluded.amount, orders.amount),
            status=CASE WHEN excluded.status = 'unknown' THEN orders.status ELSE excluded.status END,
            status_text=COALESCE(NULLIF(excluded.status_text, ''), orders.status_text),
            order_time=COALESCE(excluded.order_time, orders.order_time),
            tracking_no=COALESCE(excluded.tracking_no, orders.tracking_no),
            url=COALESCE(NULLIF(excluded.url, ''), orders.url),
            product_url=COALESCE(NULLIF(excluded.product_url, ''), orders.product_url),
            image_url=COALESCE(NULLIF(excluded.image_url, ''), orders.image_url),
            logistics_text=COALESCE(NULLIF(excluded.logistics_text, ''), orders.logistics_text),
            raw_json=COALESCE(excluded.raw_json, orders.raw_json),
            updated_at=excluded.updated_at
        """,
        (
            platform,
            platform_order_id,
            title or "",
            shop or "",
            amount,
            status or "unknown",
            status_text or "",
            order_time,
            tracking_no or None,
            url or None,
            product_url,
            image_url,
            logistics_text,
            raw_json,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM orders WHERE platform=? AND platform_order_id=?",
        (platform, platform_order_id),
    ).fetchone()
    order_id = int(row["id"])
    if tracking_no:
        _number, carrier, carrier_name = extract_tracking(logistics_text or title or "")
        ensure_shipment(
            conn,
            tracking_no=tracking_no,
            carrier=carrier,
            carrier_name=carrier_name,
            platform=platform,
            order_id=order_id,
        )
        link_order_shipment(
            conn,
            order_id=order_id,
            tracking_no=tracking_no,
            source="order",
        )
        conn.execute(
            "UPDATE shipments SET order_id=COALESCE(order_id, ?), platform=? WHERE tracking_no=?",
            (order_id, platform, tracking_no.upper()),
        )
    _fts_insert(
        conn,
        doc_type="order",
        doc_id=str(order_id),
        title=title or "",
        body=" ".join([shop or "", status_text or "", tracking_no or "", platform]),
        platform=platform,
        occurred_at=order_time,
    )
    return order_id


def upsert_shipment(
    conn: sqlite3.Connection,
    *,
    tracking_no: str,
    carrier: str = "",
    carrier_name: str = "",
    platform: str = "",
    status: str = "unknown",
    status_text: str = "",
    pickup_code: str = "",
    location: str = "",
    last_event_at: str | None = None,
    order_id: int | None = None,
    raw: Any = None,
) -> tuple[int, bool]:
    """Upsert a shipment; returns (rowid, status_changed).

    Status follows event chronology rather than a monotonic rank, so a newer
    "delivered" can correct an older "returned"/"in_transit". Older SMS events
    may still be stored as parcel_events, but they cannot roll the status back.
    """
    tracking_no = (tracking_no or "").strip().upper()
    if not tracking_no:
        raise ValueError("tracking_no is required")
    now = utc_now()
    event_at = normalize_time(last_event_at) or now
    status = (status or "unknown").lower()
    if status not in SHIPMENT_STATUSES:
        status = "unknown"
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None

    existing = conn.execute(
        "SELECT * FROM shipments WHERE tracking_no=?", (tracking_no,)
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO shipments(tracking_no, carrier, carrier_name, platform, status,
                                  status_text, pickup_code, location, last_event_at,
                                  order_id, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracking_no,
                carrier or "",
                carrier_name or "",
                (platform or "").lower(),
                status,
                status_text or "",
                pickup_code or "",
                location or "",
                event_at,
                order_id,
                raw_json,
                now,
                now,
            ),
        )
        changed = True
    else:
        old_status = existing["status"] or "unknown"
        old_event_at = existing["last_event_at"]
        stale = bool(old_event_at and event_at < old_event_at)
        effective_status = old_status if (stale or status == "unknown") else status
        changed = effective_status != old_status

        if stale:
            # Keep the newer status/pickup/location, but accept stable metadata
            # from the delayed event (carrier, platform, order link).
            conn.execute(
                """
                UPDATE shipments SET
                    carrier=COALESCE(NULLIF(?, ''), carrier),
                    carrier_name=COALESCE(NULLIF(?, ''), carrier_name),
                    platform=CASE WHEN platform = '' THEN ? ELSE platform END,
                    order_id=COALESCE(?, order_id),
                    raw_json=COALESCE(?, raw_json),
                    updated_at=?
                WHERE tracking_no=?
                """,
                (
                    carrier or "",
                    carrier_name or "",
                    (platform or "").lower(),
                    order_id,
                    raw_json,
                    now,
                    tracking_no,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE shipments SET
                    carrier=COALESCE(NULLIF(?, ''), carrier),
                    carrier_name=COALESCE(NULLIF(?, ''), carrier_name),
                    platform=CASE WHEN platform = '' THEN ? ELSE platform END,
                    status=?,
                    status_text=COALESCE(NULLIF(?, ''), status_text),
                    pickup_code=COALESCE(NULLIF(?, ''), pickup_code),
                    location=COALESCE(NULLIF(?, ''), location),
                    last_event_at=?,
                    order_id=COALESCE(?, order_id),
                    raw_json=COALESCE(?, raw_json),
                    updated_at=?
                WHERE tracking_no=?
                """,
                (
                    carrier or "",
                    carrier_name or "",
                    (platform or "").lower(),
                    effective_status,
                    status_text or "",
                    pickup_code or "",
                    location or "",
                    event_at,
                    order_id,
                    raw_json,
                    now,
                    tracking_no,
                ),
            )
    row = conn.execute(
        "SELECT rowid AS rid FROM shipments WHERE tracking_no=?", (tracking_no,)
    ).fetchone()
    return int(row["rid"]), changed


def update_order_status(
    conn: sqlite3.Connection,
    *,
    status: str,
    status_text: str = "",
    order_id: int | None = None,
    platform: str | None = None,
    platform_order_id: str | None = None,
    tracking_no: str | None = None,
) -> int | None:
    """Update an order's status (used for refunds, which are not parcel states)."""
    row = None
    if order_id is not None:
        row = conn.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone()
    elif platform and platform_order_id:
        row = conn.execute(
            "SELECT id FROM orders WHERE platform=? AND platform_order_id=?",
            (platform, platform_order_id),
        ).fetchone()
    elif tracking_no:
        row = conn.execute(
            "SELECT id FROM orders WHERE tracking_no=? ORDER BY id DESC LIMIT 1",
            (tracking_no.upper(),),
        ).fetchone()
    if row is None:
        return None
    target_id = int(row["id"])
    conn.execute(
        "UPDATE orders SET status=?, status_text=?, updated_at=? WHERE id=?",
        (status, status_text or "", utc_now(), target_id),
    )
    order = conn.execute("SELECT * FROM orders WHERE id=?", (target_id,)).fetchone()
    if order:
        _fts_insert(
            conn,
            doc_type="order",
            doc_id=str(target_id),
            title=order["title"] or "",
            body=" ".join(
                part
                for part in (
                    order["shop"],
                    status_text,
                    order["tracking_no"],
                    order["platform"],
                )
                if part
            ),
            platform=order["platform"],
            occurred_at=order["order_time"],
        )
    return target_id


def add_parcel_event(
    conn: sqlite3.Connection,
    *,
    tracking_no: str,
    event_time: str | None,
    context: str,
    status: str = "",
    source: str = "",
) -> bool:
    tracking_no = (tracking_no or "").strip().upper()
    if not tracking_no or not context:
        return False
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO parcel_events(tracking_no, event_time, context, status, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tracking_no, normalize_time(event_time), context, status or "", source or "", utc_now()),
        )
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def record_sms(
    conn: sqlite3.Connection,
    *,
    text: str,
    sender: str = "",
    received_at: str | None = None,
    parsed: dict[str, Any],
) -> tuple[int, bool]:
    text = (text or "").strip()
    digest = sha256(text)
    now = utc_now()
    existing = conn.execute("SELECT id FROM sms_events WHERE hash=?", (digest,)).fetchone()
    if existing:
        return int(existing["id"]), False
    cur = conn.execute(
        """
        INSERT INTO sms_events(hash, raw_text, sender, received_at, platform, status,
                               tracking_no, carrier, pickup_code, location, order_id,
                               ignored, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            digest,
            text,
            sender or "",
            normalize_time(received_at) or now,
            parsed.get("platform") or "",
            parsed.get("status") or "unknown",
            parsed.get("tracking_no") or "",
            parsed.get("carrier") or "",
            parsed.get("pickup_code") or "",
            parsed.get("location") or "",
            parsed.get("order_id") or "",
            1 if parsed.get("ignored") else 0,
            now,
        ),
    )
    event_id = int(cur.lastrowid)
    _fts_insert(
        conn,
        doc_type="sms",
        doc_id=str(event_id),
        title="短信动态",
        body=text,
        platform=parsed.get("platform") or "",
        occurred_at=normalize_time(received_at) or now,
    )
    return event_id, True


def record_capture(
    conn: sqlite3.Connection,
    *,
    platform: str,
    text: str,
    image_path: str = "",
    captured_at: str | None = None,
) -> tuple[int, bool]:
    text = (text or "").strip()
    digest = sha256(f"{platform}|{text}")
    existing = conn.execute("SELECT id FROM captures WHERE hash=?", (digest,)).fetchone()
    if existing:
        return int(existing["id"]), False
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO captures(hash, platform, text, image_path, captured_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            digest,
            (platform or "douyin").lower(),
            text,
            image_path or "",
            normalize_time(captured_at) or now,
            now,
        ),
    )
    capture_id = int(cur.lastrowid)
    _fts_insert(
        conn,
        doc_type="capture",
        doc_id=str(capture_id),
        title="抓屏",
        body=text,
        platform=(platform or "douyin").lower(),
        occurred_at=normalize_time(captured_at) or now,
    )
    return capture_id, True


def add_alert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body: str = "",
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO alerts(kind, title, body, created_at) VALUES (?, ?, ?, ?)",
        (kind, title, body, normalize_time(created_at) or utc_now()),
    )
    return int(cur.lastrowid)


def start_collector_run(conn: sqlite3.Connection, platform: str) -> int:
    cur = conn.execute(
        "INSERT INTO collector_runs(platform, started_at, status) VALUES (?, ?, 'running')",
        (platform, utc_now()),
    )
    return int(cur.lastrowid)


def enqueue_command(
    conn: sqlite3.Connection, *, platform: str = "jd", mode: str = "recent"
) -> int:
    cur = conn.execute(
        "INSERT INTO collector_commands(platform, mode, status, created_at) "
        "VALUES (?, ?, 'pending', ?)",
        (platform, mode, utc_now()),
    )
    return int(cur.lastrowid)


def claim_command(conn: sqlite3.Connection, worker: str) -> dict[str, Any] | None:
    """Atomically claim the oldest pending command."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM collector_commands WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE collector_commands SET status='running', claimed_at=?, worker=? WHERE id=?",
            (utc_now(), worker, row["id"]),
        )
        conn.commit()
        result = dict(row)
        result["status"] = "running"
        result["worker"] = worker
        return result
    except Exception:
        conn.rollback()
        raise


def finish_command(
    conn: sqlite3.Connection,
    command_id: int,
    *,
    status: str = "done",
    message: str = "",
) -> None:
    conn.execute(
        "UPDATE collector_commands SET status=?, finished_at=?, message=? WHERE id=?",
        (status, utc_now(), message[:2000], command_id),
    )


def upsert_push_subscription(
    conn: sqlite3.Connection,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str = "",
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO push_subscriptions(endpoint, p256dh, auth, user_agent, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            p256dh=excluded.p256dh,
            auth=excluded.auth,
            user_agent=excluded.user_agent,
            updated_at=excluded.updated_at
        """,
        (endpoint, p256dh, auth, user_agent[:300], now, now),
    )


def delete_push_subscription(conn: sqlite3.Connection, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def list_push_subscriptions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM push_subscriptions ORDER BY updated_at DESC"
        ).fetchall()
    ]


def list_commands(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM collector_commands ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    ]


def heartbeat_worker(
    conn: sqlite3.Connection,
    *,
    name: str,
    version: str = "",
    chrome_ok: bool = False,
    message: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO collector_workers(name, last_seen, version, chrome_ok, message)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_seen=excluded.last_seen,
            version=excluded.version,
            chrome_ok=excluded.chrome_ok,
            message=excluded.message
        """,
        (name, utc_now(), version, 1 if chrome_ok else 0, message[:500]),
    )


def get_worker(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM collector_workers ORDER BY last_seen DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def insert_collector_run(
    conn: sqlite3.Connection,
    *,
    platform: str,
    started_at: str | None,
    finished_at: str | None,
    status: str,
    items: int = 0,
    captcha: bool = False,
    message: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO collector_runs(platform, started_at, finished_at, status, items, captcha, message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            normalize_time(started_at) or utc_now(),
            normalize_time(finished_at),
            status or "unknown",
            int(items or 0),
            1 if captcha else 0,
            (message or "")[:2000],
        ),
    )
    return int(cur.lastrowid)


def finish_collector_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    items: int = 0,
    captcha: bool = False,
    message: str = "",
) -> None:
    conn.execute(
        """
        UPDATE collector_runs SET finished_at=?, status=?, items=?, captcha=?, message=?
        WHERE id=?
        """,
        (utc_now(), status, items, 1 if captcha else 0, message[:2000], run_id),
    )


def _document_text(conn: sqlite3.Connection, doc_type: str, doc_id: str) -> str:
    try:
        ident = int(doc_id)
    except (TypeError, ValueError):
        return ""
    if doc_type == "order":
        row = conn.execute(
            "SELECT title, shop, status_text, tracking_no FROM orders WHERE id=?",
            (ident,),
        ).fetchone()
        if row:
            return " · ".join(
                part for part in (row["title"], row["shop"], row["status_text"], row["tracking_no"]) if part
            )
    elif doc_type == "capture":
        row = conn.execute("SELECT text FROM captures WHERE id=?", (ident,)).fetchone()
        if row:
            return row["text"]
    elif doc_type == "sms":
        row = conn.execute("SELECT raw_text FROM sms_events WHERE id=?", (ident,)).fetchone()
        if row:
            return row["raw_text"]
    return ""


def make_snippet(text: str, query: str, *, length: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    if len(text) <= length:
        return text
    probe = (query or "").strip()
    position = -1
    if probe:
        position = text.lower().find(probe.lower())
    if position < 0:
        for token in tokenize(probe):
            if len(token) < 2:
                continue
            position = text.find(token)
            if position >= 0:
                break
    if position < 0:
        return text[:length] + "…"
    start = max(0, position - 24)
    end = min(len(text), start + length)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def search(
    conn: sqlite3.Connection, query: str, *, limit: int = 50, platform: str | None = None
) -> list[dict[str, Any]]:
    tokens = tokenize_query(query)
    if not tokens:
        return []
    # Quote tokens for FTS5 and OR them: matching more terms ranks higher.
    fts_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    params: list[Any] = [fts_query]
    platform_sql = ""
    if platform:
        platform_sql = " AND platform = ?"
        params.append(platform.lower())
    params.append(max(1, min(limit, 200)))
    sql = f"""
        SELECT doc_type, doc_id, title, platform, occurred_at,
               bm25(search_fts) AS score
        FROM search_fts
        WHERE search_fts MATCH ?{platform_sql}
        ORDER BY score
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        rows = _search_fallback(conn, tokens, platform=platform, limit=limit)
    results = []
    for row in rows:
        item = dict(row)
        item["snippet"] = make_snippet(
            _document_text(conn, item["doc_type"], item["doc_id"]), query
        )
        results.append(item)
    return results


def _search_fallback(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    platform: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    where = " AND ".join("tokens LIKE ?" for _ in tokens)
    params: list[Any] = [f"%{token}%" for token in tokens]
    if platform:
        where += " AND platform = ?"
        params.append(platform.lower())
    params.append(max(1, min(limit, 200)))
    sql = f"""
        SELECT doc_type, doc_id, title, platform, occurred_at, 0.0 AS score
        FROM search_docs
        WHERE {where}
        ORDER BY occurred_at DESC
        LIMIT ?
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def list_orders(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    platform: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if platform:
        where.append("platform = ?")
        params.append(platform.lower())
    if status:
        where.append("status = ?")
        params.append(status)
    sql = (
        "SELECT id, platform, platform_order_id, title, shop, amount, status, "
        "status_text, order_time, tracking_no, url, product_url, image_url, "
        "logistics_text, created_at, updated_at, "
        "(SELECT COUNT(*) FROM order_shipments os WHERE os.order_id=orders.id) "
        "AS shipment_count FROM orders"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(order_time, created_at) DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 200)), max(0, offset)])
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_shipments(
    conn: sqlite3.Connection, *, active_only: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    sql = (
        "SELECT s.tracking_no, s.carrier, s.carrier_name, s.platform, s.status, "
        "s.status_text, s.pickup_code, s.location, s.last_event_at, s.order_id, "
        "s.created_at, s.updated_at, "
        "(SELECT GROUP_CONCAT(o.title, ' / ') FROM order_shipments os "
        " JOIN orders o ON o.id=os.order_id WHERE os.tracking_no=s.tracking_no) "
        "AS order_titles FROM shipments s"
    )
    if active_only:
        sql += " WHERE s.status NOT IN ('delivered','returned','refunded')"
    sql += " ORDER BY s.updated_at DESC LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (max(1, min(limit, 300)),)).fetchall()]


def get_shipment(conn: sqlite3.Connection, tracking_no: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM shipments WHERE tracking_no=?", (tracking_no.upper(),)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result.pop("raw_json", None)
    titles = conn.execute(
        "SELECT GROUP_CONCAT(o.title, ' / ') AS titles FROM order_shipments os "
        "JOIN orders o ON o.id=os.order_id WHERE os.tracking_no=?",
        (tracking_no.upper(),),
    ).fetchone()
    result["order_titles"] = (titles["titles"] if titles else "") or ""
    result["events"] = [
        dict(event)
        for event in conn.execute(
            "SELECT * FROM parcel_events WHERE tracking_no=? "
            "ORDER BY COALESCE(event_time, created_at) DESC LIMIT 100",
            (tracking_no.upper(),),
        ).fetchall()
    ]
    return result


def list_sms(
    conn: sqlite3.Connection, *, limit: int = 50, include_ignored: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sms_events"
    if not include_ignored:
        sql += " WHERE ignored = 0"
    sql += " ORDER BY COALESCE(received_at, created_at) DESC LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (max(1, min(limit, 200)),)).fetchall()]


def list_captures(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM captures ORDER BY COALESCE(captured_at, created_at) DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    ]


def list_alerts(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    ]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, params: Iterable[Any] = ()) -> int:
        row = conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row else 0

    by_platform = {
        row["platform"]: int(row["n"])
        for row in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM orders GROUP BY platform"
        ).fetchall()
    }
    return {
        "orders": one("SELECT COUNT(*) FROM orders"),
        "shipments": one("SELECT COUNT(*) FROM shipments"),
        "active_shipments": one(
            "SELECT COUNT(*) FROM shipments WHERE status NOT IN ('delivered','returned','refunded')"
        ),
        "sms": one("SELECT COUNT(*) FROM sms_events WHERE ignored=0"),
        "captures": one("SELECT COUNT(*) FROM captures"),
        "alerts": one("SELECT COUNT(*) FROM alerts WHERE read=0"),
        "orders_by_platform": by_platform,
        "last_sms": (
            dict(
                conn.execute(
                    "SELECT received_at, platform, status, tracking_no, pickup_code, raw_text "
                    "FROM sms_events WHERE ignored=0 ORDER BY COALESCE(received_at, created_at) DESC LIMIT 1"
                ).fetchone()
                or {}
            )
            or None
        ),
        "collector_runs": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM collector_runs ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
        ],
    }


def export_all(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        "orders",
        "shipments",
        "parcel_events",
        "sms_events",
        "captures",
        "alerts",
        "collector_runs",
    ]
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        for table in tables
    }

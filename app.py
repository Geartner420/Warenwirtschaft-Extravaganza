#!/usr/bin/env python3
"""Kleines Warenwirtschaftssystem mit Standardbibliothek und SQLite."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import html
import ipaddress
import io
import json
import ssl
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


APP_TITLE = "Warenwirtschaft Extravaganza"
DB_PATH = Path(__file__).with_name("warenwirtschaft.sqlite3")
DB_BACKUP_DIR = Path(__file__).with_name("datenbank_backups")
TLS_DIR = Path(__file__).with_name("https_zertifikate")
TLS_CERT_PATH = TLS_DIR / "warenwirtschaft-local.crt"
TLS_KEY_PATH = TLS_DIR / "warenwirtschaft-local.key"
TLS_HOSTS_PATH = TLS_DIR / "warenwirtschaft-local.hosts"
TLS_CONFIG_PATH = TLS_DIR / "warenwirtschaft-local.openssl.cnf"
LOCAL_DOMAIN = "warenwirtschaft.test"
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = DB_TIMEOUT_SECONDS * 1000
DB_BACKUP_RETENTION = 30
DB_BACKUP_MIN_INTERVAL = timedelta(hours=24)
DB_BACKUP_MIN_SECONDS = 60
BARCODE_PREFIX = "WX"
USER_BARCODE_PREFIX = "WU"
LOCATION_BARCODE_PREFIX = "WO"
CONSUMABLE_CATEGORY = "Verbrauchsmaterial"
MATERIAL_SORTS = {
    "name": ("LOWER(m.name)", "LOWER(m.category)"),
    "barcode": ("LOWER(COALESCE(m.barcode, ''))",),
    "category": ("LOWER(m.category)", "LOWER(m.name)"),
    "owner": ("LOWER(m.owner)", "LOWER(m.name)"),
    "destination": ("LOWER(m.destination)", "LOWER(m.name)"),
    "total": ("m.quantity_total", "LOWER(m.name)"),
    "available": ("m.quantity_available", "LOWER(m.name)"),
    "loaned": ("quantity_loaned", "LOWER(m.name)"),
}
NUMERIC_MATERIAL_SORTS = {"total", "available", "loaned"}
CODE128_PATTERNS = (
    "212222",
    "222122",
    "222221",
    "121223",
    "121322",
    "131222",
    "122213",
    "122312",
    "132212",
    "221213",
    "221312",
    "231212",
    "112232",
    "122132",
    "122231",
    "113222",
    "123122",
    "123221",
    "223211",
    "221132",
    "221231",
    "213212",
    "223112",
    "312131",
    "311222",
    "321122",
    "321221",
    "312212",
    "322112",
    "322211",
    "212123",
    "212321",
    "232121",
    "111323",
    "131123",
    "131321",
    "112313",
    "132113",
    "132311",
    "211313",
    "231113",
    "231311",
    "112133",
    "112331",
    "132131",
    "113123",
    "113321",
    "133121",
    "313121",
    "211331",
    "231131",
    "213113",
    "213311",
    "213131",
    "311123",
    "311321",
    "331121",
    "312113",
    "312311",
    "332111",
    "314111",
    "221411",
    "431111",
    "111224",
    "111422",
    "121124",
    "121421",
    "141122",
    "141221",
    "112214",
    "112412",
    "122114",
    "122411",
    "142112",
    "142211",
    "241211",
    "221114",
    "413111",
    "241112",
    "134111",
    "111242",
    "121142",
    "121241",
    "114212",
    "124112",
    "124211",
    "411212",
    "421112",
    "421211",
    "212141",
    "214121",
    "412121",
    "111143",
    "111341",
    "131141",
    "114113",
    "114311",
    "411113",
    "411311",
    "113141",
    "114131",
    "311141",
    "411131",
    "211412",
    "211214",
    "211232",
    "2331112",
)


class AppError(Exception):
    """Fehler, die dem Nutzer direkt angezeigt werden koennen."""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def h(value: object) -> str:
    return html.escape("" if value is None else str(value))


def json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def normalize_barcode(value: str) -> str:
    return "".join((value or "").strip().upper().split())


def material_barcode(material_id: int) -> str:
    return f"{BARCODE_PREFIX}{material_id:08d}"


def user_barcode(user_id: int) -> str:
    return f"{USER_BARCODE_PREFIX}{user_id:08d}"


def location_barcode(location_id: int) -> str:
    return f"{LOCATION_BARCODE_PREFIX}{location_id:08d}"


def split_scan_codes(value: str) -> list[str]:
    parts = (value or "").replace(",", "\n").replace(";", "\n").split()
    codes = [normalize_barcode(part) for part in parts]
    return [code for code in codes if code]


def code128_values(value: str) -> list[int]:
    normalized = normalize_barcode(value)
    if not normalized:
        raise AppError("Barcode darf nicht leer sein.")
    for char in normalized:
        if not 32 <= ord(char) <= 127:
            raise AppError("Der Barcode enthaelt ein Zeichen, das Code 128 nicht darstellen kann.")

    start_b = 104
    values = [start_b]
    values.extend(ord(char) - 32 for char in normalized)
    checksum = (start_b + sum(index * code for index, code in enumerate(values[1:], start=1))) % 103
    values.append(checksum)
    values.append(106)
    return values


def barcode_svg(value: str, height: int = 48, show_text: bool = True) -> str:
    normalized = normalize_barcode(value)
    values = code128_values(normalized)
    quiet_zone = 10
    text_height = 16 if show_text else 0
    modules = quiet_zone * 2 + sum(
        sum(int(width) for width in CODE128_PATTERNS[code]) for code in values
    )
    bar_height = height
    svg_height = bar_height + text_height
    x = quiet_zone
    rects = []

    for code in values:
        is_bar = True
        for width_text in CODE128_PATTERNS[code]:
            width = int(width_text)
            if is_bar:
                rects.append(f'<rect x="{x}" y="0" width="{width}" height="{bar_height}"/>')
            x += width
            is_bar = not is_bar

    text = ""
    if show_text:
        text = (
            f'<text x="{modules / 2:.1f}" y="{bar_height + 12}" '
            'text-anchor="middle" font-family="monospace" font-size="10">'
            f"{h(normalized)}</text>"
        )

    return (
        f'<svg class="barcode-svg" viewBox="0 0 {modules} {svg_height}" '
        'role="img" aria-label="Barcode">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<g fill="#111">{"".join(rects)}{text}</g>'
        "</svg>"
    )


def material_barcode_cell(row: sqlite3.Row) -> str:
    barcode = row["barcode"]
    if not barcode:
        return '<span class="subline">Kein Barcode</span>'
    return (
        '<div class="barcode-cell">'
        f"{barcode_svg(barcode, height=32, show_text=False)}"
        f'<span class="barcode-text">{h(barcode)}</span>'
        "</div>"
    )


def user_barcode_cell(row: sqlite3.Row) -> str:
    barcode = row["barcode"]
    if not barcode:
        return '<span class="subline">Kein Code</span>'
    return (
        '<div class="barcode-cell">'
        f"{barcode_svg(barcode, height=32, show_text=False)}"
        f'<span class="barcode-text">{h(barcode)}</span>'
        "</div>"
    )


def location_barcode_cell(row: sqlite3.Row) -> str:
    barcode = row["barcode"]
    if not barcode:
        return '<span class="subline">Kein Code</span>'
    return (
        '<div class="barcode-cell">'
        f"{barcode_svg(barcode, height=32, show_text=False)}"
        f'<span class="barcode-text">{h(barcode)}</span>'
        "</div>"
    )


def positive_int(value: str, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{field_name}: Bitte eine ganze Zahl eingeben.") from exc
    if number <= 0:
        raise AppError(f"{field_name}: Die Menge muss groesser als 0 sein.")
    return number


def required(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise AppError(f"{field_name} darf nicht leer sein.")
    return value


def is_consumable_category(category: str) -> bool:
    return (category or "").strip().casefold() == CONSUMABLE_CATEGORY.casefold()


def normalize_material_sort(sort_key: str | None, sort_dir: str | None) -> tuple[str, str]:
    key = sort_key if sort_key in MATERIAL_SORTS else "name"
    direction = "desc" if sort_dir == "desc" else "asc"
    return key, direction


def material_order_clause(sort_key: str, sort_dir: str) -> str:
    sql_dir = "DESC" if sort_dir == "desc" else "ASC"
    order_parts = [f"{expression} {sql_dir}" for expression in MATERIAL_SORTS[sort_key]]
    order_parts.append("m.id ASC")
    return ", ".join(order_parts)


def safe_return_path(value: str) -> str:
    value = (value or "").strip()
    if not value.startswith("/"):
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.path not in {"/", "/mobile"}:
        return "/"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def backup_sort_key(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def prune_database_backups() -> None:
    backups = sorted(
        DB_BACKUP_DIR.glob("warenwirtschaft-*.sqlite3"),
        key=backup_sort_key,
        reverse=True,
    )
    for backup in backups[DB_BACKUP_RETENTION:]:
        try:
            backup.unlink()
        except OSError as exc:
            print(f"Warnung: Alte Datenbanksicherung konnte nicht geloescht werden: {exc}")


def backup_database_if_due(
    reason: str = "startup",
    min_interval: timedelta = DB_BACKUP_MIN_INTERVAL,
) -> Path | None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None

    DB_BACKUP_DIR.mkdir(exist_ok=True)
    backups = sorted(
        DB_BACKUP_DIR.glob("warenwirtschaft-*.sqlite3"),
        key=backup_sort_key,
        reverse=True,
    )
    if backups:
        latest = datetime.fromtimestamp(backup_sort_key(backups[0]))
        if datetime.now() - latest < min_interval:
            return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(char for char in reason if char.isalnum() or char in "-_") or "backup"
    backup_path = DB_BACKUP_DIR / f"warenwirtschaft-{safe_reason}-{timestamp}.sqlite3"

    source = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    target = sqlite3.connect(backup_path)
    failed = False
    try:
        source.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        source.backup(target, pages=1000, sleep=0.05)
    except Exception:
        failed = True
        raise
    finally:
        target.close()
        source.close()
        if failed:
            try:
                backup_path.unlink()
            except OSError:
                pass

    prune_database_backups()
    return backup_path


def run_backup_scheduler(stop_event: threading.Event, interval: timedelta) -> None:
    wait_seconds = max(DB_BACKUP_MIN_SECONDS, interval.total_seconds())
    while not stop_event.wait(wait_seconds):
        try:
            backup_path = backup_database_if_due("scheduled", interval)
            if backup_path:
                print(f"Regelmaessige Datenbanksicherung erstellt: {backup_path}")
        except Exception as exc:
            print(f"Warnung: Regelmaessige Datenbanksicherung fehlgeschlagen: {exc}")


def ensure_database_healthy(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(
            "Die SQLite-Integritaetspruefung ist fehlgeschlagen. "
            "Bitte die letzte Sicherung aus datenbank_backups pruefen."
        )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    conn.execute("PRAGMA journal_size_limit = 67108864")
    conn.execute("PRAGMA cache_size = -20000")
    return conn


@contextmanager
def write_transaction() -> sqlite3.Connection:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_stock_movement_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stock_movements_created
            ON stock_movements(created_at DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stock_movements_material_created
            ON stock_movements(material_id, created_at DESC)
        """
    )


def ensure_stock_movements_issue_action(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'stock_movements'
        """
    ).fetchone()
    table_sql = (row["sql"] if row else "") or ""
    if "'issue'" in table_sql:
        return

    conn.execute("DROP INDEX IF EXISTS idx_stock_movements_created")
    conn.execute("DROP INDEX IF EXISTS idx_stock_movements_material_created")
    conn.execute("ALTER TABLE stock_movements RENAME TO stock_movements_old")
    conn.execute(
        """
        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES materials(id),
            action TEXT NOT NULL CHECK (action IN ('in', 'out', 'loan', 'return', 'issue')),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO stock_movements (id, material_id, action, quantity, note, created_at)
        SELECT id, material_id, action, quantity, note, created_at
        FROM stock_movements_old
        """
    )
    conn.execute("DROP TABLE stock_movements_old")
    ensure_stock_movement_indexes(conn)


def init_db() -> None:
    try:
        backup_path = backup_database_if_due("startup")
        if backup_path:
            print(f"Datenbanksicherung erstellt: {backup_path}")
    except (OSError, sqlite3.DatabaseError) as exc:
        print(f"Warnung: Datenbanksicherung konnte nicht erstellt werden: {exc}")

    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT '',
                quantity_total INTEGER NOT NULL CHECK (quantity_total >= 0),
                quantity_available INTEGER NOT NULL CHECK (quantity_available >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT UNIQUE,
                name TEXT NOT NULL UNIQUE,
                contact TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT UNIQUE,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                location_id INTEGER NOT NULL REFERENCES locations(id),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                status TEXT NOT NULL CHECK (status IN ('active', 'returned')),
                loaned_at TEXT NOT NULL,
                returned_at TEXT,
                note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS stock_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                action TEXT NOT NULL CHECK (action IN ('in', 'out', 'loan', 'return', 'issue')),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_material_name ON materials(name);
            CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
            CREATE INDEX IF NOT EXISTS idx_materials_lookup
                ON materials(LOWER(name), LOWER(category), LOWER(owner), LOWER(destination));
            CREATE INDEX IF NOT EXISTS idx_materials_sort_name
                ON materials(LOWER(name), LOWER(category), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_barcode
                ON materials(LOWER(COALESCE(barcode, '')), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_category
                ON materials(LOWER(category), LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_owner
                ON materials(LOWER(owner), LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_destination
                ON materials(LOWER(destination), LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_total
                ON materials(quantity_total, LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_materials_sort_available
                ON materials(quantity_available, LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_loans_material_status
                ON loans(material_id, status);
            CREATE INDEX IF NOT EXISTS idx_loans_status_material
                ON loans(status, material_id);
            CREATE INDEX IF NOT EXISTS idx_loans_status_loaned
                ON loans(status, loaned_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_stock_movements_created
                ON stock_movements(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_stock_movements_material_created
                ON stock_movements(material_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_users_name_lower
                ON users(LOWER(name), id);
            CREATE INDEX IF NOT EXISTS idx_locations_name_lower
                ON locations(LOWER(name), id);

            CREATE TRIGGER IF NOT EXISTS trg_materials_quantity_valid_insert
            BEFORE INSERT ON materials
            WHEN NEW.quantity_total < 0
              OR NEW.quantity_available < 0
              OR NEW.quantity_available > NEW.quantity_total
            BEGIN
                SELECT RAISE(ABORT, 'ungueltiger Materialbestand');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_materials_quantity_valid_update
            BEFORE UPDATE OF quantity_total, quantity_available ON materials
            WHEN NEW.quantity_total < 0
              OR NEW.quantity_available < 0
              OR NEW.quantity_available > NEW.quantity_total
            BEGIN
                SELECT RAISE(ABORT, 'ungueltiger Materialbestand');
            END;
            """
        )
        material_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(materials)").fetchall()
        }
        if "barcode" not in material_columns:
            conn.execute("ALTER TABLE materials ADD COLUMN barcode TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_material_barcode ON materials(barcode)")

        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "barcode" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN barcode TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_barcode ON users(barcode)")

        location_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(locations)").fetchall()
        }
        if "barcode" not in location_columns:
            conn.execute("ALTER TABLE locations ADD COLUMN barcode TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_location_barcode ON locations(barcode)")

        ensure_stock_movements_issue_action(conn)
        ensure_stock_movement_indexes(conn)
        ensure_material_barcodes(conn)
        ensure_user_barcodes(conn)
        ensure_location_barcodes(conn)
        conn.execute("PRAGMA optimize")
        ensure_database_healthy(conn)


def ensure_material_barcodes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM materials WHERE barcode IS NULL OR TRIM(barcode) = '' ORDER BY id"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE materials SET barcode = ?, updated_at = ? WHERE id = ?",
            (material_barcode(row["id"]), now_iso(), row["id"]),
        )


def ensure_user_barcodes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM users WHERE barcode IS NULL OR TRIM(barcode) = '' ORDER BY id"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE users SET barcode = ? WHERE id = ?",
            (user_barcode(row["id"]), row["id"]),
        )


def ensure_location_barcodes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM locations WHERE barcode IS NULL OR TRIM(barcode) = '' ORDER BY id"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE locations SET barcode = ? WHERE id = ?",
            (location_barcode(row["id"]), row["id"]),
        )


def get_view_data(
    material_sort: str = "name",
    material_dir: str = "asc",
) -> dict[str, list[sqlite3.Row] | dict[str, int]]:
    material_sort, material_dir = normalize_material_sort(material_sort, material_dir)
    order_clause = material_order_clause(material_sort, material_dir)
    with connect() as conn:
        materials = conn.execute(
            f"""
            SELECT
                m.*,
                COALESCE(active_loans.quantity_loaned, 0) AS quantity_loaned
            FROM materials m
            LEFT JOIN (
                SELECT material_id, SUM(quantity) AS quantity_loaned
                FROM loans
                WHERE status = 'active'
                GROUP BY material_id
            ) active_loans ON active_loans.material_id = m.id
            ORDER BY {order_clause}
            """
        ).fetchall()
        users = conn.execute(
            "SELECT * FROM users ORDER BY LOWER(name), id"
        ).fetchall()
        locations = conn.execute(
            "SELECT * FROM locations ORDER BY LOWER(name), id"
        ).fetchall()
        active_loans = conn.execute(
            """
            SELECT
                l.*,
                m.name AS material_name,
                m.category AS material_category,
                u.name AS user_name,
                loc.name AS location_name
            FROM loans l
            JOIN materials m ON m.id = l.material_id
            JOIN users u ON u.id = l.user_id
            JOIN locations loc ON loc.id = l.location_id
            WHERE l.status = 'active'
            ORDER BY l.loaned_at DESC, l.id DESC
            """
        ).fetchall()
        recent_movements = conn.execute(
            """
            SELECT sm.*, m.name AS material_name
            FROM stock_movements sm
            JOIN materials m ON m.id = sm.material_id
            ORDER BY sm.created_at DESC, sm.id DESC
            LIMIT 8
            """
        ).fetchall()

    stats = {
        "material_count": len(materials),
        "available_sum": sum(row["quantity_available"] for row in materials),
        "loaned_sum": sum(row["quantity_loaned"] for row in materials),
        "active_loan_count": len(active_loans),
        "user_count": len(users),
        "location_count": len(locations),
    }
    return {
        "materials": materials,
        "users": users,
        "locations": locations,
        "active_loans": active_loans,
        "recent_movements": recent_movements,
        "stats": stats,
    }


def add_material(form: dict[str, str]) -> str:
    name = required(form.get("name", ""), "Material")
    category = required(form.get("category", ""), "Kategorie")
    owner = required(form.get("owner", ""), "Besitzer")
    destination = required(form.get("destination", ""), "Bestimmungsort")
    quantity = positive_int(form.get("quantity", ""), "Anzahl")
    timestamp = now_iso()

    with write_transaction() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM materials
            WHERE LOWER(name) = LOWER(?)
              AND LOWER(category) = LOWER(?)
              AND LOWER(owner) = LOWER(?)
              AND LOWER(destination) = LOWER(?)
            LIMIT 1
            """,
            (name, category, owner, destination),
        ).fetchone()

        if existing:
            barcode = existing["barcode"] or material_barcode(existing["id"])
            conn.execute(
                """
                UPDATE materials
                SET quantity_total = quantity_total + ?,
                    quantity_available = quantity_available + ?,
                    barcode = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (quantity, quantity, barcode, timestamp, existing["id"]),
            )
            material_id = existing["id"]
            message = (
                f"{quantity} x {name} wurde zum bestehenden Bestand gebucht. "
                f"Barcode: {barcode}"
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO materials (
                    name, category, owner, destination,
                    quantity_total, quantity_available, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    category,
                    owner,
                    destination,
                    quantity,
                    quantity,
                    timestamp,
                    timestamp,
                ),
            )
            material_id = cursor.lastrowid
            barcode = material_barcode(material_id)
            conn.execute(
                "UPDATE materials SET barcode = ? WHERE id = ?",
                (barcode, material_id),
            )
            message = f"{quantity} x {name} wurde eingebucht. Barcode: {barcode}"

        conn.execute(
            """
            INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
            VALUES (?, 'in', ?, ?, ?)
            """,
            (material_id, quantity, "Einbuchung", timestamp),
        )

    return message


def remove_material(form: dict[str, str]) -> str:
    material_id = positive_int(form.get("material_id", ""), "Material")
    quantity = positive_int(form.get("quantity", ""), "Anzahl")
    note = (form.get("note", "") or "").strip()
    timestamp = now_iso()

    with write_transaction() as conn:
        material = conn.execute(
            "SELECT * FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        if not material:
            raise AppError("Das ausgewaehlte Material existiert nicht.")
        if quantity > material["quantity_available"]:
            raise AppError(
                "So viel ist nicht frei verfuegbar. Erst Rueckgaben buchen oder eine kleinere Menge waehlen."
            )

        cursor = conn.execute(
            """
            UPDATE materials
            SET quantity_total = quantity_total - ?,
                quantity_available = quantity_available - ?,
                updated_at = ?
            WHERE id = ?
              AND quantity_total >= ?
              AND quantity_available >= ?
            """,
            (quantity, quantity, timestamp, material_id, quantity, quantity),
        )
        if cursor.rowcount != 1:
            raise AppError(
                "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
            )
        conn.execute(
            """
            INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
            VALUES (?, 'out', ?, ?, ?)
            """,
            (material_id, quantity, note or "Ausbuchung", timestamp),
        )

    return f"{quantity} x {material['name']} wurde ausgebucht."


def update_material_destination(form: dict[str, str]) -> str:
    material_id = positive_int(form.get("material_id", ""), "Material")
    destination = required(form.get("destination", ""), "Bestimmungsort")
    timestamp = now_iso()

    with write_transaction() as conn:
        material = conn.execute(
            "SELECT * FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        if not material:
            raise AppError("Das ausgewaehlte Material existiert nicht.")

        if material["destination"] == destination:
            return f"Bestimmungsort fuer {material['name']} ist bereits {destination}."

        conn.execute(
            """
            UPDATE materials
            SET destination = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (destination, timestamp, material_id),
        )

    return f"Bestimmungsort fuer {material['name']} wurde auf {destination} gesetzt."


def update_material(form: dict[str, str]) -> str:
    material_id = positive_int(form.get("material_id", ""), "Material")
    name = required(form.get("name", ""), "Material")
    category = required(form.get("category", ""), "Kategorie")
    owner = required(form.get("owner", ""), "Besitzer")
    destination = required(form.get("destination", ""), "Bestimmungsort")
    timestamp = now_iso()

    with write_transaction() as conn:
        material = conn.execute(
            "SELECT * FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        if not material:
            raise AppError("Das ausgewaehlte Material existiert nicht.")

        duplicate = conn.execute(
            """
            SELECT id
            FROM materials
            WHERE id <> ?
              AND LOWER(name) = LOWER(?)
              AND LOWER(category) = LOWER(?)
              AND LOWER(owner) = LOWER(?)
              AND LOWER(destination) = LOWER(?)
            LIMIT 1
            """,
            (material_id, name, category, owner, destination),
        ).fetchone()
        if duplicate:
            raise AppError(
                "Ein Artikel mit diesen Angaben existiert bereits. "
                "Bitte dort Bestand einbuchen oder andere Angaben waehlen."
            )

        if (
            material["name"] == name
            and material["category"] == category
            and material["owner"] == owner
            and material["destination"] == destination
        ):
            return f"Artikel {material['name']} ist bereits aktuell."

        conn.execute(
            """
            UPDATE materials
            SET name = ?,
                category = ?,
                owner = ?,
                destination = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (name, category, owner, destination, timestamp, material_id),
        )

    return f"Artikel {name} wurde aktualisiert."


def book_by_barcode(form: dict[str, str]) -> str:
    barcode = normalize_barcode(required(form.get("barcode", ""), "Barcode"))
    action = required(form.get("action", ""), "Buchungsart")
    quantity = positive_int(form.get("quantity", ""), "Anzahl")
    note = (form.get("note", "") or "").strip()
    timestamp = now_iso()

    if action not in {"in", "out"}:
        raise AppError("Unbekannte Buchungsart.")

    with write_transaction() as conn:
        material = conn.execute(
            "SELECT * FROM materials WHERE barcode = ?",
            (barcode,),
        ).fetchone()
        if not material:
            raise AppError(f"Kein Material mit Barcode {barcode} gefunden.")

        if action == "out" and quantity > material["quantity_available"]:
            raise AppError(
                "So viel ist nicht frei verfuegbar. Erst Rueckgaben buchen oder eine kleinere Menge waehlen."
            )

        quantity_delta = quantity if action == "in" else -quantity
        default_note = "Scan-Einbuchung" if action == "in" else "Scan-Ausbuchung"
        if action == "out":
            cursor = conn.execute(
                """
                UPDATE materials
                SET quantity_total = quantity_total - ?,
                    quantity_available = quantity_available - ?,
                    updated_at = ?
                WHERE id = ?
                  AND quantity_total >= ?
                  AND quantity_available >= ?
                """,
                (quantity, quantity, timestamp, material["id"], quantity, quantity),
            )
            if cursor.rowcount != 1:
                raise AppError(
                    "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
                )
        else:
            conn.execute(
                """
                UPDATE materials
                SET quantity_total = quantity_total + ?,
                    quantity_available = quantity_available + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (quantity_delta, quantity_delta, timestamp, material["id"]),
            )
        conn.execute(
            """
            INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (material["id"], action, quantity, note or default_note, timestamp),
        )

    label = "eingebucht" if action == "in" else "ausgebucht"
    return f"{quantity} x {material['name']} wurde per Scan {label}."


def book_batch_by_codes(form: dict[str, str]) -> str:
    action = required(form.get("action", ""), "Buchungsart")
    codes = split_scan_codes(required(form.get("codes", ""), "Codes"))
    note = (form.get("note", "") or "").strip()
    timestamp = now_iso()

    if action not in {"in", "out", "loan"}:
        raise AppError("Unbekannte Buchungsart.")

    with write_transaction() as conn:
        material_counts: dict[int, int] = {}
        scanned_user: sqlite3.Row | None = None
        scanned_location: sqlite3.Row | None = None
        unknown_codes: list[str] = []

        for code in codes:
            material = conn.execute(
                "SELECT id FROM materials WHERE barcode = ?",
                (code,),
            ).fetchone()
            if material:
                material_id = material["id"]
                material_counts[material_id] = material_counts.get(material_id, 0) + 1
                continue

            user = conn.execute(
                "SELECT * FROM users WHERE barcode = ?",
                (code,),
            ).fetchone()
            if user:
                if scanned_user and scanned_user["id"] != user["id"]:
                    raise AppError("Im Stapel sind mehrere unterschiedliche Nutzer-Codes.")
                scanned_user = user
                continue

            location = conn.execute(
                "SELECT * FROM locations WHERE barcode = ?",
                (code,),
            ).fetchone()
            if location:
                if scanned_location and scanned_location["id"] != location["id"]:
                    raise AppError("Im Stapel sind mehrere unterschiedliche Ort-Codes.")
                scanned_location = location
                continue

            unknown_codes.append(code)

        if unknown_codes:
            raise AppError(f"Unbekannte Codes: {', '.join(unknown_codes)}")
        if not material_counts:
            raise AppError("Mindestens ein Material-Code muss gescannt werden.")
        if action != "loan" and (scanned_user or scanned_location):
            raise AppError("Nutzer- und Ort-Codes koennen nur beim Verleih mitgescannt werden.")

        total_quantity = sum(material_counts.values())
        position_count = len(material_counts)

        if action in {"in", "out"}:
            for material_id, quantity in material_counts.items():
                material = conn.execute(
                    "SELECT * FROM materials WHERE id = ?",
                    (material_id,),
                ).fetchone()
                if not material:
                    raise AppError("Ein gescanntes Material existiert nicht mehr.")

                if action == "out" and quantity > material["quantity_available"]:
                    raise AppError(
                        f"Von {material['name']} sind nur {material['quantity_available']} frei verfuegbar."
                    )

                if action == "out":
                    cursor = conn.execute(
                        """
                        UPDATE materials
                        SET quantity_total = quantity_total - ?,
                            quantity_available = quantity_available - ?,
                            updated_at = ?
                        WHERE id = ?
                          AND quantity_total >= ?
                          AND quantity_available >= ?
                        """,
                        (quantity, quantity, timestamp, material_id, quantity, quantity),
                    )
                    if cursor.rowcount != 1:
                        raise AppError(
                            "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
                        )
                else:
                    conn.execute(
                        """
                        UPDATE materials
                        SET quantity_total = quantity_total + ?,
                            quantity_available = quantity_available + ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (quantity, quantity, timestamp, material_id),
                    )

                default_note = "Stapel-Scan-Einbuchung" if action == "in" else "Stapel-Scan-Ausbuchung"
                conn.execute(
                    """
                    INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (material_id, action, quantity, note or default_note, timestamp),
                )

            label = "eingebucht" if action == "in" else "ausgebucht"
            return f"{total_quantity} Einheit(en) aus {position_count} Position(en) wurden per Stapel-Scan {label}."

        user = scanned_user
        if not user and (form.get("user_id", "") or "").strip():
            user_id = positive_int(form.get("user_id", ""), "Nutzer")
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("Fuer den Verleih muss ein Nutzer-Code gescannt werden.")

        location = scanned_location
        if not location and (form.get("location_id", "") or "").strip():
            location_id = positive_int(form.get("location_id", ""), "Ort / Baustelle")
            location = conn.execute(
                "SELECT * FROM locations WHERE id = ?",
                (location_id,),
            ).fetchone()
        if not location:
            raise AppError("Fuer den Verleih muss ein Ort-Code gescannt oder ein Ort ausgewaehlt werden.")
        location_id = location["id"]

        issued_quantity = 0
        loaned_quantity = 0
        for material_id, quantity in material_counts.items():
            material = conn.execute(
                "SELECT * FROM materials WHERE id = ?",
                (material_id,),
            ).fetchone()
            if not material:
                raise AppError("Ein gescanntes Material existiert nicht mehr.")
            if quantity > material["quantity_available"]:
                raise AppError(
                    f"Von {material['name']} sind nur {material['quantity_available']} frei verfuegbar."
                )

            if is_consumable_category(material["category"]):
                cursor = conn.execute(
                    """
                    UPDATE materials
                    SET quantity_total = quantity_total - ?,
                        quantity_available = quantity_available - ?,
                        updated_at = ?
                    WHERE id = ?
                      AND quantity_total >= ?
                      AND quantity_available >= ?
                    """,
                    (quantity, quantity, timestamp, material_id, quantity, quantity),
                )
                if cursor.rowcount != 1:
                    raise AppError(
                        "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
                    )
                movement_note = f"Stapel-Ausgabe an {user['name']} fuer {location['name']}"
                if note:
                    movement_note = f"{movement_note}: {note}"
                conn.execute(
                    """
                    INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
                    VALUES (?, 'issue', ?, ?, ?)
                    """,
                    (material_id, quantity, movement_note, timestamp),
                )
                issued_quantity += quantity
                continue

            cursor = conn.execute(
                """
                UPDATE materials
                SET quantity_available = quantity_available - ?,
                    updated_at = ?
                WHERE id = ?
                  AND quantity_available >= ?
                """,
                (quantity, timestamp, material_id, quantity),
            )
            if cursor.rowcount != 1:
                raise AppError(
                    "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
                )
            conn.execute(
                """
                INSERT INTO loans (
                    material_id, user_id, location_id, quantity, status, loaned_at, note
                )
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (material_id, user["id"], location_id, quantity, timestamp, note),
            )
            conn.execute(
                """
                INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
                VALUES (?, 'loan', ?, ?, ?)
                """,
                (
                    material_id,
                    quantity,
                    f"Stapel-Verleih an {user['name']} fuer {location['name']}",
                    timestamp,
                ),
            )
            loaned_quantity += quantity

    parts = []
    if loaned_quantity:
        parts.append(f"{loaned_quantity} verliehen")
    if issued_quantity:
        parts.append(f"{issued_quantity} ausgegeben")
    return f"Stapel-Scan fuer {user['name']}: {', '.join(parts)}."


def create_user_from_form(form: dict[str, str]) -> sqlite3.Row:
    name = required(form.get("name", ""), "Name")
    contact = (form.get("contact", "") or "").strip()
    notes = (form.get("notes", "") or "").strip()
    timestamp = now_iso()

    try:
        with write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (name, contact, notes, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, contact, notes, timestamp),
            )
            conn.execute(
                "UPDATE users SET barcode = ? WHERE id = ?",
                (user_barcode(cursor.lastrowid), cursor.lastrowid),
            )
            user = conn.execute(
                "SELECT * FROM users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise AppError("Diesen Nutzer gibt es bereits.") from exc

    if not user:
        raise AppError("Der Nutzer konnte nicht angelegt werden.")
    return user


def add_user(form: dict[str, str]) -> str:
    user = create_user_from_form(form)
    return f"Nutzer {user['name']} wurde angelegt. Code: {user['barcode']}. Das Label ist in der Nutzerliste druckbar."


def create_location_from_form(form: dict[str, str]) -> sqlite3.Row:
    name = required(form.get("name", ""), "Ort / Baustelle")
    timestamp = now_iso()

    try:
        with write_transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO locations (name, created_at) VALUES (?, ?)",
                (name, timestamp),
            )
            conn.execute(
                "UPDATE locations SET barcode = ? WHERE id = ?",
                (location_barcode(cursor.lastrowid), cursor.lastrowid),
            )
            location = conn.execute(
                "SELECT * FROM locations WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise AppError("Diesen Ort bzw. diese Baustelle gibt es bereits.") from exc

    if not location:
        raise AppError("Der Ort bzw. die Baustelle konnte nicht angelegt werden.")
    return location


def add_location(form: dict[str, str]) -> str:
    location = create_location_from_form(form)
    return f"Ort / Baustelle {location['name']} wurde angelegt. Code: {location['barcode']}. Das Label ist in der Ortsliste druckbar."


def add_loan(form: dict[str, str]) -> str:
    material_id = positive_int(form.get("material_id", ""), "Material")
    user_id = positive_int(form.get("user_id", ""), "Nutzer")
    location_id = positive_int(form.get("location_id", ""), "Ort / Baustelle")
    quantity = positive_int(form.get("quantity", ""), "Anzahl")
    note = (form.get("note", "") or "").strip()
    timestamp = now_iso()

    with write_transaction() as conn:
        material = conn.execute(
            "SELECT * FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        if not material:
            raise AppError("Das ausgewaehlte Material existiert nicht.")
        if quantity > material["quantity_available"]:
            raise AppError("Von diesem Material ist nicht genug frei verfuegbar.")

        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("Der ausgewaehlte Nutzer existiert nicht.")

        location = conn.execute(
            "SELECT * FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        if not location:
            raise AppError("Der ausgewaehlte Ort existiert nicht.")

        if is_consumable_category(material["category"]):
            cursor = conn.execute(
                """
                UPDATE materials
                SET quantity_total = quantity_total - ?,
                    quantity_available = quantity_available - ?,
                    updated_at = ?
                WHERE id = ?
                  AND quantity_total >= ?
                  AND quantity_available >= ?
                """,
                (quantity, quantity, timestamp, material_id, quantity, quantity),
            )
            if cursor.rowcount != 1:
                raise AppError(
                    "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
                )
            movement_note = f"Ausgabe an {user['name']} fuer {location['name']}"
            if note:
                movement_note = f"{movement_note}: {note}"
            conn.execute(
                """
                INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
                VALUES (?, 'issue', ?, ?, ?)
                """,
                (material_id, quantity, movement_note, timestamp),
            )
            return f"{quantity} x {material['name']} wurde an {user['name']} ausgegeben."

        cursor = conn.execute(
            """
            UPDATE materials
            SET quantity_available = quantity_available - ?,
                updated_at = ?
            WHERE id = ?
              AND quantity_available >= ?
            """,
            (quantity, timestamp, material_id, quantity),
        )
        if cursor.rowcount != 1:
            raise AppError(
                "Der Bestand wurde parallel geaendert. Bitte die Menge erneut pruefen."
            )
        conn.execute(
            """
            INSERT INTO loans (
                material_id, user_id, location_id, quantity, status, loaned_at, note
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (material_id, user_id, location_id, quantity, timestamp, note),
        )
        conn.execute(
            """
            INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
            VALUES (?, 'loan', ?, ?, ?)
            """,
            (
                material_id,
                quantity,
                f"Verliehen an {user['name']} fuer {location['name']}",
                timestamp,
            ),
        )

    return f"{quantity} x {material['name']} wurde an {user['name']} verliehen."


def return_loan(form: dict[str, str]) -> str:
    loan_id = positive_int(form.get("loan_id", ""), "Ausleihe")
    timestamp = now_iso()

    with write_transaction() as conn:
        loan = conn.execute(
            """
            SELECT l.*, m.name AS material_name, u.name AS user_name
            FROM loans l
            JOIN materials m ON m.id = l.material_id
            JOIN users u ON u.id = l.user_id
            WHERE l.id = ?
            """,
            (loan_id,),
        ).fetchone()
        if not loan:
            raise AppError("Die Ausleihe existiert nicht.")
        if loan["status"] != "active":
            raise AppError("Diese Ausleihe wurde bereits zurueckgebucht.")

        cursor = conn.execute(
            """
            UPDATE loans
            SET status = 'returned',
                returned_at = ?
            WHERE id = ?
              AND status = 'active'
            """,
            (timestamp, loan_id),
        )
        if cursor.rowcount != 1:
            raise AppError("Diese Ausleihe wurde bereits zurueckgebucht.")
        conn.execute(
            """
            UPDATE materials
            SET quantity_available = quantity_available + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (loan["quantity"], timestamp, loan["material_id"]),
        )
        conn.execute(
            """
            INSERT INTO stock_movements (material_id, action, quantity, note, created_at)
            VALUES (?, 'return', ?, ?, ?)
            """,
            (
                loan["material_id"],
                loan["quantity"],
                f"Rueckgabe von {loan['user_name']}",
                timestamp,
            ),
        )

    return f"{loan['quantity']} x {loan['material_name']} wurde zurueckgebucht."


def get_material_categories(materials: list[sqlite3.Row]) -> list[str]:
    categories: dict[str, str] = {}
    for row in materials:
        category = (row["category"] or "").strip()
        if category:
            categories.setdefault(category.casefold(), category)
    return sorted(categories.values(), key=str.casefold)


def render_material_category_options(materials: list[sqlite3.Row]) -> str:
    if not materials:
        return '<option value="">Erst Material anlegen</option>'

    options = ['<option value="">Kategorie waehlen</option>']
    for category in get_material_categories(materials):
        options.append(f'<option value="{h(category)}">{h(category)}</option>')
    return "\n".join(options)


def render_material_options(materials: list[sqlite3.Row], only_available: bool = False) -> str:
    if not materials:
        return '<option value="">Erst Material anlegen</option>'

    options = ['<option value="">Erst Kategorie waehlen</option>']
    for row in materials:
        disabled = only_available and row["quantity_available"] <= 0
        label = (
            f"{row['barcode']} | {row['name']} | "
            f"{row['quantity_available']} verfuegbar / {row['quantity_total']} gesamt"
        )
        attr = ' data-unavailable="1" disabled' if disabled else ""
        options.append(
            f'<option value="{row["id"]}" data-category="{h(row["category"])}"{attr}>'
            f"{h(label)}</option>"
        )
    return "\n".join(options)


def render_material_picker(
    materials: list[sqlite3.Row],
    only_available: bool = False,
    disabled: str = "",
    material_select_id: str = "",
) -> str:
    disabled_attr = " disabled" if disabled.strip() or not materials else ""
    material_id_attr = f' id="{h(material_select_id)}"' if material_select_id else ""
    picker_disabled = ' data-picker-disabled="1"' if disabled_attr else ""
    return f"""
                    <div class="material-picker full" data-material-picker>
                        <label>Kategorie
                            <select required data-material-category{disabled_attr}>
                                {render_material_category_options(materials)}
                            </select>
                        </label>
                        <label>Material
                            <select name="material_id"{material_id_attr} required data-material-select{picker_disabled}{disabled_attr}>
                                {render_material_options(materials, only_available=only_available)}
                            </select>
                        </label>
                    </div>
    """


def render_user_options(users: list[sqlite3.Row]) -> str:
    if not users:
        return '<option value="">Erst Nutzer anlegen</option>'
    options = ['<option value="">Nutzer waehlen</option>']
    options.extend(f'<option value="{row["id"]}">{h(row["name"])}</option>' for row in users)
    return "\n".join(options)


def render_location_options(locations: list[sqlite3.Row]) -> str:
    if not locations:
        return '<option value="">Erst Ort / Baustelle anlegen</option>'
    options = ['<option value="">Ort / Baustelle waehlen</option>']
    options.extend(f'<option value="{row["id"]}">{h(row["name"])}</option>' for row in locations)
    return "\n".join(options)


def material_sort_url(sort_key: str, current_sort: str, current_dir: str) -> str:
    if sort_key == current_sort:
        next_dir = "desc" if current_dir == "asc" else "asc"
    else:
        next_dir = "desc" if sort_key in NUMERIC_MATERIAL_SORTS else "asc"
    return "/?" + urlencode({"sort": sort_key, "dir": next_dir})


def render_sort_header(
    label: str,
    sort_key: str,
    current_sort: str,
    current_dir: str,
    number: bool = False,
) -> str:
    is_active = sort_key == current_sort
    aria_sort = ""
    if is_active:
        aria_sort = f' aria-sort="{"descending" if current_dir == "desc" else "ascending"}"'
    th_class = ' class="number"' if number else ""
    link_classes = ["sort-link"]
    if is_active:
        link_classes.extend(["active", current_dir])
    return (
        f"<th{th_class}{aria_sort}>"
        f'<a class="{" ".join(link_classes)}" '
        f'href="{h(material_sort_url(sort_key, current_sort, current_dir))}">'
        f"{h(label)}</a></th>"
    )


def get_destination_choices(
    materials: list[sqlite3.Row],
    locations: list[sqlite3.Row],
) -> list[str]:
    choices = {
        destination.strip()
        for destination in [
            *(row["name"] for row in locations),
            *(row["destination"] for row in materials),
        ]
        if destination and destination.strip()
    }
    return sorted(choices, key=str.casefold)


def render_destination_select(
    row: sqlite3.Row,
    destination_choices: list[str],
    current_path: str,
) -> str:
    current_destination = row["destination"]
    choices = list(destination_choices)
    if current_destination and current_destination not in choices:
        choices.insert(0, current_destination)

    options = []
    for destination in choices:
        selected = " selected" if destination == current_destination else ""
        options.append(f'<option value="{h(destination)}"{selected}>{h(destination)}</option>')

    if not options:
        options.append('<option value="">Kein Ort vorhanden</option>')

    return (
        '<form method="post" action="/materials/destination" class="destination-form">'
        f'<input type="hidden" name="material_id" value="{row["id"]}">'
        f'<input type="hidden" name="next" value="{h(current_path)}">'
        f'<select name="destination" aria-label="Bestimmungsort fuer {h(row["name"])}" '
        "required data-auto-submit>"
        f'{"".join(options)}'
        "</select>"
        '<button type="submit" class="small-button destination-save">Speichern</button>'
        "</form>"
    )


def serialize_material(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "barcode": row["barcode"],
        "name": row["name"],
        "category": row["category"],
        "owner": row["owner"],
        "destination": row["destination"],
        "quantityTotal": row["quantity_total"],
        "quantityAvailable": row["quantity_available"],
        "quantityLoaned": row["quantity_loaned"] if "quantity_loaned" in row.keys() else 0,
    }


def serialize_user(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "barcode": row["barcode"],
        "name": row["name"],
        "contact": row["contact"],
        "notes": row["notes"],
    }


def serialize_location(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "barcode": row["barcode"],
        "name": row["name"],
    }


def render_material_rows(
    materials: list[sqlite3.Row],
    destination_choices: list[str],
    current_path: str,
) -> str:
    if not materials:
        return '<tr><td colspan="9" class="empty">Noch kein Material erfasst.</td></tr>'

    rows = []
    for row in materials:
        rows.append(
            f"""
            <tr>
                <td>
                    <strong>{h(row['name'])}</strong>
                    <span class="subline">ID {row['id']}</span>
                </td>
                <td>
                    {material_barcode_cell(row)}
                    <a class="subline label-link" href="/labels/material/{row['id']}">Label drucken</a>
                </td>
                <td>{h(row['category'])}</td>
                <td>{h(row['owner'])}</td>
                <td>{render_destination_select(row, destination_choices, current_path)}</td>
                <td class="number">{row['quantity_total']}</td>
                <td class="number">{row['quantity_available']}</td>
                <td class="number">{row['quantity_loaned']}</td>
                <td class="action-cell">
                    <button
                        type="button"
                        class="small-button secondary-button"
                        data-edit-material="{row['id']}"
                    >Bearbeiten</button>
                </td>
            </tr>
            """
        )
    return "\n".join(rows)


def render_loan_rows(active_loans: list[sqlite3.Row]) -> str:
    if not active_loans:
        return '<tr><td colspan="7" class="empty">Aktuell ist nichts verliehen.</td></tr>'

    rows = []
    for row in active_loans:
        rows.append(
            f"""
            <tr>
                <td>
                    <strong>{h(row['material_name'])}</strong>
                    <span class="subline">{h(row['material_category'])}</span>
                </td>
                <td>{h(row['user_name'])}</td>
                <td>{h(row['location_name'])}</td>
                <td class="number">{row['quantity']}</td>
                <td>{h(row['loaned_at'])}</td>
                <td>{h(row['note'])}</td>
                <td>
                    <form method="post" action="/loans/return" class="inline-form">
                        <input type="hidden" name="loan_id" value="{row['id']}">
                        <button type="submit" class="small-button">Rueckgabe</button>
                    </form>
                </td>
            </tr>
            """
        )
    return "\n".join(rows)


def render_user_rows(users: list[sqlite3.Row]) -> str:
    if not users:
        return '<tr><td colspan="5" class="empty">Noch keine Nutzer angelegt.</td></tr>'

    rows = []
    for row in users:
        rows.append(
            f"""
            <tr>
                <td><strong>{h(row['name'])}</strong></td>
                <td>
                    {user_barcode_cell(row)}
                    <a class="subline label-link" href="/labels/user/{row['id']}">Label drucken</a>
                </td>
                <td>{h(row['contact'])}</td>
                <td>{h(row['notes'])}</td>
                <td>{h(row['created_at'])}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def render_location_rows(locations: list[sqlite3.Row]) -> str:
    if not locations:
        return '<tr><td colspan="3" class="empty">Noch keine Orte oder Baustellen angelegt.</td></tr>'

    return "\n".join(
        f"""
        <tr>
            <td><strong>{h(row['name'])}</strong></td>
            <td>
                {location_barcode_cell(row)}
                <a class="subline label-link" href="/labels/location/{row['id']}">Label drucken</a>
            </td>
            <td>{h(row['created_at'])}</td>
        </tr>
        """
        for row in locations
    )


def render_movement_rows(movements: list[sqlite3.Row]) -> str:
    if not movements:
        return '<tr><td colspan="5" class="empty">Noch keine Buchungen vorhanden.</td></tr>'

    labels = {
        "in": "Einbuchung",
        "out": "Ausbuchung",
        "loan": "Verleih",
        "return": "Rueckgabe",
        "issue": "Ausgabe",
    }
    rows = []
    for row in movements:
        rows.append(
            f"""
            <tr>
                <td>{h(row['created_at'])}</td>
                <td>{h(labels.get(row['action'], row['action']))}</td>
                <td>{h(row['material_name'])}</td>
                <td class="number">{row['quantity']}</td>
                <td>{h(row['note'])}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def get_label_materials(material_id: int | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if material_id is None:
            return conn.execute(
                """
                SELECT *
                FROM materials
                ORDER BY LOWER(name), LOWER(category), id
                """
            ).fetchall()
        return conn.execute(
            "SELECT * FROM materials WHERE id = ?",
            (material_id,),
        ).fetchall()


def get_label_users(user_id: int | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if user_id is None:
            return conn.execute(
                """
                SELECT *
                FROM users
                ORDER BY LOWER(name), id
                """
            ).fetchall()
        return conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchall()


def get_label_locations(location_id: int | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if location_id is None:
            return conn.execute(
                """
                SELECT *
                FROM locations
                ORDER BY LOWER(name), id
                """
            ).fetchall()
        return conn.execute(
            "SELECT * FROM locations WHERE id = ?",
            (location_id,),
        ).fetchall()


def render_label_cards(materials: list[sqlite3.Row]) -> str:
    if not materials:
        return '<p class="empty">Keine Labels vorhanden.</p>'

    cards = []
    for row in materials:
        cards.append(
            f"""
            <article class="label-card">
                <strong>{h(row['name'])}</strong>
                <span>{h(row['category'])} | {h(row['owner'])}</span>
                {barcode_svg(row['barcode'], height=52, show_text=True)}
                <small>{h(row['destination'])}</small>
            </article>
            """
        )
    return "\n".join(cards)


def render_user_label_cards(users: list[sqlite3.Row]) -> str:
    if not users:
        return '<p class="empty">Keine Nutzer-Labels vorhanden.</p>'

    cards = []
    for row in users:
        cards.append(
            f"""
            <article class="label-card">
                <strong>{h(row['name'])}</strong>
                <span>Nutzer</span>
                {barcode_svg(row['barcode'], height=52, show_text=True)}
                <small>{h(row['contact'])}</small>
            </article>
            """
        )
    return "\n".join(cards)


def render_location_label_cards(locations: list[sqlite3.Row]) -> str:
    if not locations:
        return '<p class="empty">Keine Ort-Labels vorhanden.</p>'

    cards = []
    for row in locations:
        cards.append(
            f"""
            <article class="label-card">
                <strong>{h(row['name'])}</strong>
                <span>Ort / Baustelle</span>
                {barcode_svg(row['barcode'], height=52, show_text=True)}
                <small>Einsatzort</small>
            </article>
            """
        )
    return "\n".join(cards)


def render_labels_page(title: str, cards_html: str) -> str:
    return f"""<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{h(title)} - {h(APP_TITLE)}</title>
    <style>
        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: #f7f7f4;
            color: #202124;
            font: 14px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 18px 24px;
            background: #1f2d2a;
            color: #fff;
        }}

        h1 {{
            margin: 0;
            font-size: 22px;
            letter-spacing: 0;
        }}

        a, button {{
            border: 0;
            border-radius: 8px;
            background: #146c63;
            color: #fff;
            cursor: pointer;
            font: inherit;
            font-weight: 700;
            padding: 10px 13px;
            text-decoration: none;
        }}

        .actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}

        main {{
            padding: 18px;
        }}

        .label-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
            gap: 12px;
        }}

        .label-card {{
            display: grid;
            gap: 5px;
            min-height: 145px;
            padding: 10px;
            border: 1px dashed #777;
            border-radius: 6px;
            background: #fff;
        }}

        .label-card strong {{
            font-size: 14px;
        }}

        .label-card span,
        .label-card small {{
            color: #535d59;
        }}

        .barcode-svg {{
            width: 100%;
            height: 68px;
            display: block;
        }}

        .empty {{
            color: #697077;
        }}

        @media print {{
            header {{
                display: none;
            }}

            body {{
                background: #fff;
            }}

            main {{
                padding: 0;
            }}

            .label-grid {{
                grid-template-columns: repeat(3, 1fr);
                gap: 0;
            }}

            .label-card {{
                break-inside: avoid;
                border: 1px dashed #999;
                border-radius: 0;
                min-height: 38mm;
                padding: 5mm;
            }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>{h(title)}</h1>
        <div class="actions">
            <a href="/">Zurueck</a>
            <button type="button" onclick="window.print()">Drucken</button>
        </div>
    </header>
    <main>
        <section class="label-grid">
            {cards_html}
        </section>
    </main>
</body>
</html>"""


CODE128_CAMERA_FALLBACK_SCRIPT = r"""
        const CODE128_FALLBACK_PATTERNS = __CODE128_PATTERNS__;

        function createCode128FallbackScanner(video, setStatus) {
            const canvas = document.createElement("canvas");
            const context = canvas.getContext("2d", { willReadFrequently: true });
            const patternCandidates = CODE128_FALLBACK_PATTERNS
                .map((pattern, code) => ({ code, widths: pattern.split("").map(Number) }));
            const startCandidates = patternCandidates.filter((candidate) => candidate.code === 104);
            const symbolCandidates = patternCandidates.filter((candidate) => candidate.code < 106);
            const stopCandidate = patternCandidates.find((candidate) => candidate.code === 106);
            const sampleRows = [0.32, 0.42, 0.5, 0.58, 0.68];
            let lastStatus = "";

            function notify(message) {
                if (message && message !== lastStatus) {
                    lastStatus = message;
                    setStatus(message);
                }
            }

            function symbolDistance(lengths, candidate) {
                const expected = candidate.widths;
                const expectedTotal = expected.reduce((sum, width) => sum + width, 0);
                const actualTotal = lengths.reduce((sum, width) => sum + width, 0);
                if (actualTotal <= 0) {
                    return Infinity;
                }
                const moduleWidth = actualTotal / expectedTotal;
                return expected.reduce((sum, width, index) => {
                    return sum + Math.abs((lengths[index] / moduleWidth) - width);
                }, 0) / expected.length;
            }

            function bestSymbol(runs, index, candidates, runCount, maxDistance) {
                if (index + runCount > runs.length) {
                    return null;
                }
                const lengths = runs.slice(index, index + runCount).map((run) => run.length);
                let best = null;
                for (const candidate of candidates) {
                    const distance = symbolDistance(lengths, candidate);
                    if (!best || distance < best.distance) {
                        best = { code: candidate.code, distance };
                    }
                }
                return best && best.distance <= maxDistance ? best : null;
            }

            function checksumMatches(codes) {
                if (codes.length < 3 || codes[0] !== 104) {
                    return false;
                }
                const checksum = codes[codes.length - 1];
                const expected = codes
                    .slice(1, -1)
                    .reduce((sum, code, index) => sum + ((index + 1) * code), codes[0]) % 103;
                return checksum === expected;
            }

            function codesToText(codes) {
                const dataCodes = codes.slice(1, -1);
                let text = "";
                for (const code of dataCodes) {
                    if (code < 0 || code > 95) {
                        return "";
                    }
                    text += String.fromCharCode(code + 32);
                }
                return text;
            }

            function decodeRuns(runs) {
                const maxDistance = 0.62;
                const stopMaxDistance = 0.45;
                for (let startIndex = 0; startIndex + 19 < runs.length; startIndex += 1) {
                    if (!runs[startIndex].black) {
                        continue;
                    }
                    const start = bestSymbol(runs, startIndex, startCandidates, 6, maxDistance);
                    if (!start) {
                        continue;
                    }

                    const codes = [start.code];
                    let index = startIndex + 6;
                    for (let symbolIndex = 0; symbolIndex < 80 && index < runs.length; symbolIndex += 1) {
                        const stop = bestSymbol(runs, index, [stopCandidate], 7, stopMaxDistance);
                        if (stop && checksumMatches(codes)) {
                            return codesToText(codes);
                        }

                        const symbol = bestSymbol(runs, index, symbolCandidates, 6, maxDistance);
                        if (!symbol) {
                            break;
                        }
                        codes.push(symbol.code);
                        index += 6;
                    }
                }
                return "";
            }

            function rowRuns(imageData, width) {
                const values = [];
                let min = 255;
                let max = 0;
                let sum = 0;
                for (let x = 0; x < width; x += 1) {
                    const offset = x * 4;
                    const value = (
                        imageData[offset] * 0.299
                        + imageData[offset + 1] * 0.587
                        + imageData[offset + 2] * 0.114
                    );
                    values.push(value);
                    min = Math.min(min, value);
                    max = Math.max(max, value);
                    sum += value;
                }

                if (max - min < 35) {
                    return [];
                }

                const mean = sum / width;
                const threshold = Math.max(min + 18, Math.min(max - 18, (mean + min + max) / 3));
                const runs = [];
                let black = values[0] < threshold;
                let length = 1;
                for (let x = 1; x < width; x += 1) {
                    const isBlack = values[x] < threshold;
                    if (isBlack === black) {
                        length += 1;
                    } else {
                        runs.push({ black, length });
                        black = isBlack;
                        length = 1;
                    }
                }
                runs.push({ black, length });

                return runs;
            }

            function decodeFrame() {
                if (!context || !video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
                    return "";
                }

                const width = Math.min(900, video.videoWidth);
                const height = Math.max(sampleRows.length, Math.round(video.videoHeight * (width / video.videoWidth)));
                canvas.width = width;
                canvas.height = height;
                context.drawImage(video, 0, 0, width, height);

                for (const rowRatio of sampleRows) {
                    const y = Math.min(height - 1, Math.max(0, Math.round(height * rowRatio)));
                    const frameRow = context.getImageData(0, y, width, 1).data;
                    const decoded = decodeRuns(rowRuns(frameRow, width));
                    if (decoded) {
                        return decoded;
                    }
                }
                return "";
            }

            if (!context) {
                notify("Dieser Browser kann das Kamerabild nicht lokal auswerten.");
            }

            return {
                isAvailable: Boolean(context),
                async detect() {
                    const value = decodeFrame();
                    return value ? [value] : [];
                },
            };
        }

        async function createCameraBarcodeReader(video, setStatus, formats) {
            const fallback = createCode128FallbackScanner(video, setStatus);
            if ("BarcodeDetector" in window) {
                try {
                    let detectorOptions = {};
                    if (typeof window.BarcodeDetector.getSupportedFormats === "function") {
                        const supported = await window.BarcodeDetector.getSupportedFormats();
                        const selectedFormats = formats.filter((format) => supported.includes(format));
                        if (selectedFormats.includes("code_128")) {
                            detectorOptions = { formats: selectedFormats };
                        } else if (fallback.isAvailable) {
                            setStatus("Browser-Scanner fehlt. Lokaler Code-128-Scanner aktiv.");
                            return fallback;
                        } else {
                            setStatus("Dieser Browser liest keine Code-128-Barcodes aus dem Kamerabild.");
                            return null;
                        }
                    }
                    const detector = new window.BarcodeDetector(detectorOptions);
                    return {
                        async detect() {
                            const results = await detector.detect(video);
                            return results.map((result) => result.rawValue);
                        },
                    };
                } catch (error) {
                    if (fallback.isAvailable) {
                        setStatus("Browser-Scanner nicht nutzbar. Lokaler Code-128-Scanner aktiv.");
                        return fallback;
                    }
                    setStatus("Barcode-Erkennung konnte nicht geladen werden.");
                    return null;
                }
            }

            if (fallback.isAvailable) {
                setStatus("Browser-Scanner fehlt. Lokaler Code-128-Scanner aktiv.");
                return fallback;
            }

            setStatus("Dieser Browser kann Barcodes nicht aus dem Kamerabild lesen.");
            return null;
        }
""".replace("__CODE128_PATTERNS__", json.dumps(CODE128_PATTERNS))


def render_page(query: dict[str, list[str]]) -> str:
    current_sort, current_dir = normalize_material_sort(
        query.get("sort", ["name"])[0],
        query.get("dir", ["asc"])[0],
    )
    current_path = "/?" + urlencode({"sort": current_sort, "dir": current_dir})
    overview_return_path = "/?" + urlencode(
        {"sort": current_sort, "dir": current_dir, "panel": "overview"}
    )
    scanner_return_path = "/?" + urlencode(
        {"sort": current_sort, "dir": current_dir, "panel": "scanner"}
    )
    data = get_view_data(current_sort, current_dir)
    materials = data["materials"]
    users = data["users"]
    locations = data["locations"]
    active_loans = data["active_loans"]
    recent_movements = data["recent_movements"]
    stats = data["stats"]
    destination_choices = get_destination_choices(materials, locations)

    message = query.get("message", [""])[0]
    kind = query.get("kind", ["ok"])[0]
    flash = ""
    if message:
        flash = f'<div class="flash {h(kind)}">{h(message)}</div>'

    material_select_disabled = "" if materials else "disabled"
    user_select_disabled = "" if users else "disabled"
    location_select_disabled = "" if locations else "disabled"
    disabled_for_loan = "" if materials and users and locations else "disabled"
    loan_hint = ""
    missing_loan_parts = []
    if not materials:
        missing_loan_parts.append("Material")
    if not users:
        missing_loan_parts.append("Nutzer")
    if not locations:
        missing_loan_parts.append("Ort bzw. Baustelle")
    if missing_loan_parts:
        loan_hint = (
            '<p class="hint">Zum Verleihen oder Ausgeben fehlt noch: '
            f'{h(", ".join(missing_loan_parts))}. '
            'Material kann bereits ausgewaehlt werden, fehlende Nutzer und Orte legst du unter '
            '<strong>Nutzer &amp; Orte</strong> an.</p>'
        )
    disabled_for_scan = ""
    scan_hint = ""
    if not materials:
        disabled_for_scan = "disabled"
        scan_hint = '<p class="hint">Zum Scannen muss zuerst mindestens ein Material angelegt sein.</p>'
    client_data = json_for_script(
        {
            "materials": [serialize_material(row) for row in materials],
            "users": [serialize_user(row) for row in users],
            "locations": [serialize_location(row) for row in locations],
        }
    )

    return f"""<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{h(APP_TITLE)}</title>
    <style>
        :root {{
            --bg: #f7f7f4;
            --surface: #ffffff;
            --ink: #202124;
            --muted: #697077;
            --line: #d9ddd6;
            --accent: #146c63;
            --accent-dark: #0e4f49;
            --warn: #a86d00;
            --danger: #a13a32;
            --soft: #e8f2ef;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: var(--bg);
            color: var(--ink);
            font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}

        header {{
            background: #1f2d2a;
            color: #fff;
            padding: 22px clamp(16px, 4vw, 48px);
            border-bottom: 5px solid #d59b2d;
        }}

        header h1 {{
            margin: 0 0 4px;
            font-size: clamp(24px, 4vw, 34px);
            letter-spacing: 0;
        }}

        header p {{
            margin: 0;
            color: #dce7e4;
        }}

        .mobile-switch {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 38px;
            margin-top: 12px;
            border-radius: 8px;
            background: #fff;
            color: #1f2d2a;
            font-weight: 750;
            padding: 8px 11px;
            text-decoration: none;
        }}

        main {{
            width: min(1180px, calc(100% - 28px));
            margin: 22px auto 60px;
        }}

        .flash {{
            padding: 12px 14px;
            margin-bottom: 16px;
            border: 1px solid var(--line);
            background: var(--soft);
            border-radius: 8px;
            font-weight: 650;
        }}

        .flash.error {{
            background: #fff0ed;
            border-color: #e2aaa2;
            color: var(--danger);
        }}

        .stats {{
            display: grid;
            grid-template-columns: repeat(6, minmax(130px, 1fr));
            gap: 10px;
            margin-bottom: 16px;
        }}

        .stat {{
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 12px;
            min-height: 78px;
        }}

        .stat span {{
            display: block;
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
        }}

        .stat strong {{
            display: block;
            margin-top: 6px;
            font-size: 26px;
        }}

        .tabs {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 18px 0;
        }}

        .tab-button, button, .download {{
            border: 0;
            border-radius: 8px;
            background: var(--accent);
            color: #fff;
            cursor: pointer;
            font-weight: 700;
            padding: 10px 13px;
            text-decoration: none;
        }}

        .tab-button {{
            background: #fff;
            border: 1px solid var(--line);
            color: var(--ink);
        }}

        .tab-button.active {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        button:hover, .download:hover {{
            background: var(--accent-dark);
        }}

        button:disabled {{
            background: #9da8a5;
            cursor: not-allowed;
        }}

        .panel {{
            display: none;
        }}

        .panel.active {{
            display: block;
        }}

        .section {{
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 8px;
            margin-bottom: 16px;
            padding: 16px;
        }}

        .section h2 {{
            margin: 0 0 12px;
            font-size: 20px;
            letter-spacing: 0;
        }}

        .grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
        }}

        form.grid-form {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }}

        label {{
            display: grid;
            gap: 5px;
            color: var(--muted);
            font-size: 13px;
            font-weight: 650;
        }}

        input, select, textarea {{
            width: 100%;
            min-height: 40px;
            border: 1px solid var(--line);
            border-radius: 7px;
            color: var(--ink);
            font: inherit;
            padding: 9px 10px;
            background: #fff;
        }}

        textarea {{
            min-height: 84px;
            resize: vertical;
        }}

        .full {{
            grid-column: 1 / -1;
        }}

        .material-picker {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }}

        .form-actions {{
            align-self: end;
        }}

        .table-wrap {{
            width: 100%;
            overflow-x: auto;
            border: 1px solid var(--line);
            border-radius: 8px;
        }}

        table {{
            width: 100%;
            min-width: 1200px;
            border-collapse: collapse;
            background: #fff;
        }}

        th, td {{
            padding: 10px 12px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
        }}

        th {{
            background: #eef1ed;
            color: #3a4542;
            font-size: 12px;
            text-transform: uppercase;
        }}

        .sort-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: inherit;
            text-decoration: none;
            white-space: nowrap;
        }}

        .sort-link::after {{
            content: "";
            width: 0;
            height: 0;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid currentColor;
            opacity: 0.35;
        }}

        .sort-link.asc::after {{
            border-top: 0;
            border-bottom: 5px solid currentColor;
            opacity: 1;
        }}

        .sort-link.desc::after {{
            opacity: 1;
        }}

        th.number .sort-link {{
            justify-content: flex-end;
            width: 100%;
        }}

        tr:last-child td {{
            border-bottom: 0;
        }}

        .number {{
            text-align: right;
            font-variant-numeric: tabular-nums;
        }}

        .subline {{
            display: block;
            color: var(--muted);
            font-size: 12px;
            margin-top: 2px;
        }}

        .empty, .hint {{
            color: var(--muted);
        }}

        .small-button {{
            padding: 7px 9px;
            font-size: 13px;
        }}

        .inline-form {{
            margin: 0;
        }}

        .action-cell {{
            min-width: 120px;
            white-space: nowrap;
        }}

        .destination-form {{
            display: flex;
            align-items: center;
            gap: 6px;
            min-width: 220px;
            margin: 0;
        }}

        .destination-form select {{
            min-height: 34px;
            padding: 6px 8px;
        }}

        .destination-save {{
            min-height: 34px;
            padding: 6px 8px;
            font-size: 12px;
        }}

        .toolbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }}

        .toolbar-actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            justify-content: flex-end;
        }}

        .barcode-cell {{
            display: grid;
            gap: 4px;
            min-width: 150px;
        }}

        .barcode-svg {{
            display: block;
            width: 150px;
            height: 34px;
        }}

        .barcode-text {{
            color: var(--ink);
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12px;
            letter-spacing: 0;
        }}

        .label-link {{
            color: var(--accent);
            font-weight: 700;
            text-decoration: none;
        }}

        .scan-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
        }}

        .scan-form {{
            display: grid;
            gap: 12px;
            padding: 14px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #fbfcfa;
        }}

        .scan-form h3 {{
            margin: 0;
            font-size: 18px;
            letter-spacing: 0;
        }}

        .batch-scan-form {{
            margin-bottom: 14px;
        }}

        .scan-list {{
            display: grid;
            gap: 8px;
            min-height: 44px;
        }}

        .scan-list-header,
        .scan-item {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 9px 10px;
            border: 1px solid var(--line);
            border-radius: 7px;
            background: #fff;
        }}

        .scan-list-header {{
            background: var(--soft);
            color: var(--accent-dark);
            font-weight: 800;
        }}

        .scan-item span {{
            color: var(--muted);
            font-size: 13px;
        }}

        .scan-input {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 18px;
        }}

        .camera-scanner {{
            display: grid;
            grid-template-columns: minmax(280px, 1.2fr) minmax(240px, 0.8fr);
            gap: 14px;
            margin-bottom: 14px;
            padding: 14px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #fbfcfa;
        }}

        .camera-preview {{
            display: grid;
            gap: 8px;
        }}

        .camera-preview video {{
            width: 100%;
            aspect-ratio: 16 / 9;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #121715;
            object-fit: cover;
        }}

        .camera-controls {{
            display: grid;
            gap: 10px;
            align-content: start;
        }}

        .button-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}

        .secondary-button {{
            background: #eef1ed;
            border: 1px solid var(--line);
            color: var(--ink);
        }}

        .secondary-button:hover {{
            background: #dde4df;
        }}

        .scanner-status {{
            min-height: 44px;
            padding: 10px;
            border: 1px solid var(--line);
            border-radius: 7px;
            background: #fff;
            color: var(--muted);
        }}

        .scanner-status strong {{
            color: var(--ink);
        }}

        .loan-dialog {{
            width: min(760px, calc(100% - 24px));
            border: 0;
            border-radius: 8px;
            padding: 0;
            box-shadow: 0 24px 70px rgba(20, 28, 26, 0.32);
        }}

        .loan-dialog::backdrop {{
            background: rgba(20, 28, 26, 0.48);
        }}

        .wizard {{
            background: #fff;
        }}

        .wizard-header {{
            display: flex;
            justify-content: space-between;
            gap: 12px;
            padding: 16px;
            border-bottom: 1px solid var(--line);
        }}

        .wizard-header h2 {{
            margin: 0;
            font-size: 20px;
            letter-spacing: 0;
        }}

        .icon-button {{
            width: 38px;
            height: 38px;
            padding: 0;
            display: inline-grid;
            place-items: center;
            background: #eef1ed;
            border: 1px solid var(--line);
            color: var(--ink);
            font-size: 22px;
            line-height: 1;
        }}

        .wizard-body {{
            display: grid;
            gap: 14px;
            padding: 16px;
        }}

        .material-summary {{
            display: grid;
            gap: 4px;
            padding: 12px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--soft);
        }}

        .action-choice-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }}

        .action-choice-button {{
            display: grid;
            gap: 5px;
            align-content: start;
            min-height: 92px;
            padding: 12px;
            text-align: left;
            background: #fff;
            border: 1px solid var(--line);
            color: var(--ink);
        }}

        .action-choice-button:hover {{
            background: var(--soft);
        }}

        .action-choice-button span {{
            color: var(--muted);
            font-size: 13px;
            font-weight: 500;
        }}

        .action-choice-button:disabled {{
            background: #eef1ed;
            color: #697077;
        }}

        .compact-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }}

        .inline-create {{
            display: grid;
            gap: 10px;
            padding: 10px;
            border: 1px dashed var(--line);
            border-radius: 8px;
            background: #fbfcfa;
        }}

        .inline-create summary {{
            cursor: pointer;
            color: var(--accent);
            font-weight: 700;
        }}

        .inline-create form {{
            display: grid;
            gap: 10px;
            margin-top: 8px;
        }}

        @media (max-width: 900px) {{
            .stats, .grid, .scan-grid, .camera-scanner, .action-choice-grid, .compact-grid, form.grid-form, .material-picker {{
                grid-template-columns: 1fr;
            }}

            main {{
                width: min(100% - 20px, 1180px);
            }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>{h(APP_TITLE)}</h1>
        <p>Material erfassen, Bestand buchen, Nutzer anlegen und Dinge sauber verleihen oder ausgeben.</p>
        <a class="mobile-switch" href="/mobile">Handy-Ansicht</a>
    </header>

    <main>
        {flash}

        <section class="stats" aria-label="Kennzahlen">
            <div class="stat"><span>Materialarten</span><strong>{stats['material_count']}</strong></div>
            <div class="stat"><span>Verfuegbar</span><strong>{stats['available_sum']}</strong></div>
            <div class="stat"><span>Verliehen</span><strong>{stats['loaned_sum']}</strong></div>
            <div class="stat"><span>Aktive Leihen</span><strong>{stats['active_loan_count']}</strong></div>
            <div class="stat"><span>Nutzer</span><strong>{stats['user_count']}</strong></div>
            <div class="stat"><span>Orte</span><strong>{stats['location_count']}</strong></div>
        </section>

        <nav class="tabs" aria-label="Bereiche">
            <button class="tab-button active" type="button" data-panel="overview">Uebersicht</button>
            <button class="tab-button" type="button" data-panel="book-in">Material einbuchen</button>
            <button class="tab-button" type="button" data-panel="book-out">Material ausbuchen</button>
            <button class="tab-button" type="button" data-panel="scanner">Scanner</button>
            <button class="tab-button" type="button" data-panel="loan">Verleih / Ausgabe</button>
            <button class="tab-button" type="button" data-panel="people">Nutzer &amp; Orte</button>
        </nav>

        <section id="overview" class="panel active">
            <div class="section">
                <div class="toolbar">
                    <h2>Materialliste</h2>
                    <div class="toolbar-actions">
                        <a class="download" href="/labels/materials">Alle Labels</a>
                        <a class="download" href="/export/material.csv">CSV Export</a>
                    </div>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                {render_sort_header("Material", "name", current_sort, current_dir)}
                                {render_sort_header("Barcode", "barcode", current_sort, current_dir)}
                                {render_sort_header("Kategorie", "category", current_sort, current_dir)}
                                {render_sort_header("Besitzer", "owner", current_sort, current_dir)}
                                {render_sort_header("Bestimmungsort", "destination", current_sort, current_dir)}
                                {render_sort_header("Gesamt", "total", current_sort, current_dir, number=True)}
                                {render_sort_header("Frei", "available", current_sort, current_dir, number=True)}
                                {render_sort_header("Verliehen", "loaned", current_sort, current_dir, number=True)}
                                <th>Aktion</th>
                            </tr>
                        </thead>
                        <tbody>{render_material_rows(materials, destination_choices, current_path)}</tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <h2>Aktiv verliehen</h2>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Material</th>
                                <th>Nutzer</th>
                                <th>Ort / Baustelle</th>
                                <th class="number">Anzahl</th>
                                <th>Seit</th>
                                <th>Notiz</th>
                                <th>Aktion</th>
                            </tr>
                        </thead>
                        <tbody>{render_loan_rows(active_loans)}</tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <h2>Letzte Buchungen</h2>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Zeit</th>
                                <th>Art</th>
                                <th>Material</th>
                                <th class="number">Anzahl</th>
                                <th>Notiz</th>
                            </tr>
                        </thead>
                        <tbody>{render_movement_rows(recent_movements)}</tbody>
                    </table>
                </div>
            </div>
        </section>

        <section id="book-in" class="panel">
            <div class="section">
                <h2>Neues Material einbuchen</h2>
                <form method="post" action="/materials/add" class="grid-form">
                    <label>Art des Materials
                        <input name="name" placeholder="z.B. Akkuschrauber Bosch GSR 18V" required>
                    </label>
                    <label>Kategorie
                        <input name="category" list="categories" placeholder="z.B. Werkzeug" required>
                    </label>
                    <label>Besitzer
                        <input name="owner" placeholder="z.B. Firma, Peter, Lager" required>
                    </label>
                    <label>Bestimmungsort
                        <input name="destination" placeholder="z.B. Lager A, Baustelle Mitte" required>
                    </label>
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required>
                    </label>
                    <div class="form-actions">
                        <button type="submit">Einbuchen</button>
                    </div>
                    <datalist id="categories">
                        <option value="Werkzeug"></option>
                        <option value="Verbrauchsmaterial"></option>
                        <option value="Maschine"></option>
                        <option value="Baumaterial"></option>
                        <option value="Schutzkleidung"></option>
                    </datalist>
                </form>
            </div>
        </section>

        <section id="book-out" class="panel">
            <div class="section">
                <h2>Material ausbuchen</h2>
                <form method="post" action="/materials/remove" class="grid-form">
                    {render_material_picker(materials, only_available=True, disabled=material_select_disabled)}
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required>
                    </label>
                    <label>Grund / Notiz
                        <input name="note" placeholder="z.B. verbraucht, defekt, verkauft">
                    </label>
                    <div class="form-actions">
                        <button type="submit">Ausbuchen</button>
                    </div>
                </form>
            </div>
        </section>

        <section id="scanner" class="panel">
            <div class="section">
                <h2>Per Scanner buchen</h2>
                {scan_hint}
                <div class="camera-scanner" data-camera-scanner>
                    <div class="camera-preview">
                        <video id="barcodeVideo" playsinline muted></video>
                        <div id="scannerStatus" class="scanner-status">Kamera aus.</div>
                    </div>
                    <div class="camera-controls">
                        <label>Kamera
                            <select id="cameraSelect" {disabled_for_scan}>
                                <option value="">Kamera suchen</option>
                            </select>
                        </label>
                        <div class="button-row">
                            <button type="button" id="startCameraButton" {disabled_for_scan}>Kamera starten</button>
                            <button type="button" id="stopCameraButton" class="secondary-button" disabled>Stopp</button>
                        </div>
                        <form id="manualLoanScanForm" class="inline-create">
                            <label>Barcode fuer Buchung
                                <input class="scan-input" id="manualLoanBarcode" placeholder="Barcode scannen oder eintippen" autocomplete="off" {disabled_for_scan} data-scan-input>
                            </label>
                            <button type="submit" {disabled_for_scan}>Auswahl oeffnen</button>
                        </form>
                    </div>
                </div>
                <form method="post" action="/scan/batch" class="scan-form batch-scan-form" id="batchScanForm">
                    <h3>Stapel-Scan</h3>
                    <input type="hidden" name="next" value="{h(scanner_return_path)}">
                    <input type="hidden" name="codes" id="batchScanCodes">
                    <div class="compact-grid">
                        <label>Buchungsart
                            <select name="action" id="batchScanAction" {disabled_for_scan}>
                                <option value="loan">Verleih / Ausgabe</option>
                                <option value="in">Einbuchen</option>
                                <option value="out">Ausbuchen</option>
                            </select>
                        </label>
                        <label>Code
                            <input class="scan-input" id="batchScanInput" placeholder="Material-, Nutzer- oder Ort-Code" autocomplete="off" {disabled_for_scan} data-scan-input>
                        </label>
                    </div>
                    <div class="compact-grid">
                        <label>Ort / Baustelle
                            <select name="location_id" id="batchScanLocation" {location_select_disabled}>{render_location_options(locations)}</select>
                        </label>
                        <label>Notiz
                            <input name="note" id="batchScanNote" placeholder="optional" {disabled_for_scan}>
                        </label>
                    </div>
                    <div class="scanner-status" id="batchScanStatus">Bereit.</div>
                    <div class="scan-list" id="batchScanList"></div>
                    <div class="button-row">
                        <button type="button" id="batchScanClear" class="secondary-button" {disabled_for_scan}>Liste leeren</button>
                        <button type="submit" id="batchScanSubmit" {disabled_for_scan}>Stapel buchen</button>
                    </div>
                </form>
                <div class="scan-grid">
                    <form method="post" action="/scan/book" class="scan-form">
                        <h3>Einbuchen</h3>
                        <input type="hidden" name="action" value="in">
                        <label>Barcode
                            <input class="scan-input" name="barcode" placeholder="Barcode scannen oder eintippen" autocomplete="off" required {disabled_for_scan} data-scan-input>
                        </label>
                        <label>Anzahl
                            <input name="quantity" type="number" min="1" step="1" value="1" required {disabled_for_scan}>
                        </label>
                        <label>Notiz
                            <input name="note" placeholder="optional" {disabled_for_scan}>
                        </label>
                        <button type="submit" {disabled_for_scan}>Einbuchen</button>
                    </form>

                    <form method="post" action="/scan/book" class="scan-form">
                        <h3>Ausbuchen</h3>
                        <input type="hidden" name="action" value="out">
                        <label>Barcode
                            <input class="scan-input" name="barcode" placeholder="Barcode scannen oder eintippen" autocomplete="off" required {disabled_for_scan} data-scan-input>
                        </label>
                        <label>Anzahl
                            <input name="quantity" type="number" min="1" step="1" value="1" required {disabled_for_scan}>
                        </label>
                        <label>Grund / Notiz
                            <input name="note" placeholder="z.B. verbraucht, defekt" {disabled_for_scan}>
                        </label>
                        <button type="submit" {disabled_for_scan}>Ausbuchen</button>
                    </form>
                </div>
            </div>
        </section>

        <section id="loan" class="panel">
            <div class="section">
                <h2>Material verleihen / ausgeben</h2>
                {loan_hint}
                <form method="post" action="/loans/add" class="grid-form">
                    {render_material_picker(materials, only_available=True, disabled=material_select_disabled)}
                    <label>Nutzer
                        <select name="user_id" required {user_select_disabled}>{render_user_options(users)}</select>
                    </label>
                    <label>Ort / Baustelle
                        <select name="location_id" required {location_select_disabled}>{render_location_options(locations)}</select>
                    </label>
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required {disabled_for_loan}>
                    </label>
                    <label>Notiz
                        <input name="note" placeholder="optional" {disabled_for_loan}>
                    </label>
                    <div class="form-actions">
                        <button type="submit" {disabled_for_loan}>Buchen</button>
                    </div>
                </form>
            </div>
        </section>

        <section id="people" class="panel">
            <div class="grid">
                <div class="section">
                    <h2>Nutzer anlegen</h2>
                    <form method="post" action="/users/add" class="grid-form">
                        <label class="full">Name
                            <input name="name" placeholder="z.B. Anna Schneider" required>
                        </label>
                        <label class="full">Kontakt
                            <input name="contact" placeholder="Telefon oder E-Mail, optional">
                        </label>
                        <label class="full">Notiz
                            <textarea name="notes" placeholder="optional"></textarea>
                        </label>
                        <div class="form-actions">
                            <button type="submit">Nutzer erstellen</button>
                        </div>
                    </form>
                </div>

                <div class="section">
                    <h2>Ort / Baustelle anlegen</h2>
                    <form method="post" action="/locations/add" class="grid-form">
                        <label class="full">Name
                            <input name="name" placeholder="z.B. Baustelle Hauptstrasse" required>
                        </label>
                        <div class="form-actions">
                            <button type="submit">Ort speichern</button>
                        </div>
                    </form>
                </div>
            </div>

            <div class="section">
                <div class="toolbar">
                    <h2>Nutzer</h2>
                    <div class="toolbar-actions">
                        <a class="download" href="/labels/users">Nutzer-Labels</a>
                    </div>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Code</th>
                                <th>Kontakt</th>
                                <th>Notiz</th>
                                <th>Angelegt</th>
                            </tr>
                        </thead>
                        <tbody>{render_user_rows(users)}</tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <div class="toolbar">
                    <h2>Orte / Baustellen</h2>
                    <div class="toolbar-actions">
                        <a class="download" href="/labels/locations">Ort-Labels</a>
                    </div>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Code</th>
                                <th>Angelegt</th>
                            </tr>
                        </thead>
                        <tbody>{render_location_rows(locations)}</tbody>
                    </table>
                </div>
            </div>
        </section>
    </main>

    <dialog id="materialEditDialog" class="loan-dialog">
        <div class="wizard">
            <div class="wizard-header">
                <div>
                    <h2 id="materialEditTitle">Artikel bearbeiten</h2>
                    <span class="subline" id="materialEditMeta"></span>
                </div>
                <button type="button" class="icon-button" id="closeMaterialEdit" aria-label="Schliessen">&times;</button>
            </div>
            <div class="wizard-body">
                <form method="post" action="/materials/update" class="grid-form" id="materialEditForm">
                    <input type="hidden" name="material_id">
                    <input type="hidden" name="next" value="{h(overview_return_path)}">
                    <label>Art des Materials
                        <input name="name" required>
                    </label>
                    <label>Kategorie
                        <input name="category" list="categories" required>
                    </label>
                    <label>Besitzer
                        <input name="owner" required>
                    </label>
                    <label>Bestimmungsort
                        <input name="destination" required>
                    </label>
                    <div class="form-actions">
                        <button type="submit">Speichern</button>
                    </div>
                </form>
            </div>
        </div>
    </dialog>

    <dialog id="scanActionDialog" class="loan-dialog">
        <div class="wizard">
            <div class="wizard-header">
                <div>
                    <h2>Aktion fuer Scan waehlen</h2>
                    <span class="subline" id="scanActionBarcode"></span>
                </div>
                <button type="button" class="icon-button" id="closeScanAction" aria-label="Schliessen">&times;</button>
            </div>
            <div class="wizard-body">
                <div class="material-summary" id="scanActionMaterial">
                    <strong>Kein Material gewaehlt</strong>
                    <span class="subline">Barcode zuerst scannen.</span>
                </div>
                <div class="scanner-status" id="scanActionStatus">Bereit.</div>
                <div class="action-choice-grid">
                    <button type="button" class="action-choice-button" id="scanActionLoan">
                        <strong>Verleih / Ausgabe</strong>
                        <span>Nutzer und Ort auswaehlen, danach verleihen oder ausgeben.</span>
                    </button>
                    <button type="button" class="action-choice-button" id="scanActionBookIn">
                        <strong>Einbuchen</strong>
                        <span>Bestand fuer diesen Barcode erhoehen.</span>
                    </button>
                    <button type="button" class="action-choice-button" id="scanActionBookOut">
                        <strong>Ausbuchen</strong>
                        <span>Frei verfuegbaren Bestand entfernen.</span>
                    </button>
                </div>
            </div>
        </div>
    </dialog>

    <dialog id="scanBookingDialog" class="loan-dialog">
        <div class="wizard">
            <div class="wizard-header">
                <div>
                    <h2 id="scanBookingTitle">Material per Scan buchen</h2>
                    <span class="subline" id="scanBookingBarcode"></span>
                </div>
                <button type="button" class="icon-button" id="closeScanBooking" aria-label="Schliessen">&times;</button>
            </div>
            <div class="wizard-body">
                <div class="material-summary" id="scanBookingMaterial">
                    <strong>Kein Material gewaehlt</strong>
                    <span class="subline">Barcode zuerst scannen.</span>
                </div>
                <div class="scanner-status" id="scanBookingStatus">Bereit.</div>
                <form method="post" action="/scan/book" class="grid-form" id="scanBookingForm">
                    <input type="hidden" name="action" id="scanBookingAction">
                    <input type="hidden" name="barcode" id="scanBookingBarcodeInput">
                    <label>Anzahl
                        <input name="quantity" id="scanBookingQuantity" type="number" min="1" step="1" value="1" required>
                    </label>
                    <label>Notiz
                        <input name="note" id="scanBookingNote" placeholder="optional">
                    </label>
                    <div class="form-actions">
                        <button type="submit" id="scanBookingSubmit">Buchen</button>
                    </div>
                </form>
            </div>
        </div>
    </dialog>

    <dialog id="loanWizard" class="loan-dialog">
        <div class="wizard">
            <div class="wizard-header">
                <div>
                    <h2 id="loanWizardTitle">Material verleihen / ausgeben</h2>
                    <span class="subline" id="loanWizardBarcode"></span>
                </div>
                <button type="button" class="icon-button" id="closeLoanWizard" aria-label="Schliessen">&times;</button>
            </div>
            <div class="wizard-body">
                <div class="material-summary" id="loanWizardMaterial">
                    <strong>Kein Material gewaehlt</strong>
                    <span class="subline">Barcode zuerst scannen.</span>
                </div>
                <div class="scanner-status" id="wizardStatus">Bereit.</div>

                <form method="post" action="/loans/add" class="grid-form" id="loanWizardForm">
                    <input type="hidden" name="material_id" id="loanWizardMaterialId">
                    <input type="hidden" name="next" value="{h(overview_return_path)}">
                    <label>Nutzer-Code
                        <input class="scan-input" id="loanWizardUserBarcode" placeholder="Nutzer-Code scannen" autocomplete="off" {user_select_disabled}>
                    </label>
                    <label>Nutzer
                        <select name="user_id" id="loanWizardUser" required {user_select_disabled}>{render_user_options(users)}</select>
                    </label>
                    <label>Ort-Code
                        <input class="scan-input" id="loanWizardLocationBarcode" placeholder="Ort-Code scannen" autocomplete="off" {location_select_disabled}>
                    </label>
                    <label>Ort / Baustelle
                        <select name="location_id" id="loanWizardLocation" required {location_select_disabled}>{render_location_options(locations)}</select>
                    </label>
                    <label>Anzahl
                        <input name="quantity" id="loanWizardQuantity" type="number" min="1" step="1" value="1" required {disabled_for_loan}>
                    </label>
                    <label>Notiz
                        <input name="note" id="loanWizardNote" placeholder="optional" {disabled_for_loan}>
                    </label>
                    <div class="form-actions">
                        <button type="submit" id="loanWizardSubmit" {disabled_for_loan}>Buchen</button>
                    </div>
                </form>

                <div class="compact-grid">
                    <details class="inline-create">
                        <summary>Neuen Nutzer erstellen</summary>
                        <form id="wizardUserForm">
                            <label>Name
                                <input name="name" placeholder="z.B. Anna Schneider" required>
                            </label>
                            <label>Kontakt
                                <input name="contact" placeholder="Telefon oder E-Mail, optional">
                            </label>
                            <label>Notiz
                                <textarea name="notes" placeholder="optional"></textarea>
                            </label>
                            <button type="submit">Nutzer erstellen</button>
                        </form>
                    </details>

                    <details class="inline-create">
                        <summary>Neue Baustelle erstellen</summary>
                        <form id="wizardLocationForm">
                            <label>Name
                                <input name="name" placeholder="z.B. Baustelle Hauptstrasse" required>
                            </label>
                            <button type="submit">Baustelle speichern</button>
                        </form>
                    </details>
                </div>
            </div>
        </div>
    </dialog>

    <script>
        const appData = {client_data};
        const buttons = Array.from(document.querySelectorAll(".tab-button"));
        const panels = Array.from(document.querySelectorAll(".panel"));
        const materialsByBarcode = new Map();
        const materialsById = new Map();
        const usersByBarcode = new Map();
        const usersById = new Map();
        const locationsByBarcode = new Map();
        const locationsById = new Map();
        const consumableCategory = "verbrauchsmaterial";

        function normalizeBarcode(value) {{
            return String(value || "").trim().toUpperCase().replace(/\s+/g, "");
        }}

        function normalizeCategory(value) {{
            return String(value || "").trim().toLocaleLowerCase("de-DE");
        }}

        function isConsumableMaterial(material) {{
            return normalizeCategory(material?.category) === consumableCategory;
        }}

        appData.materials.forEach((material) => {{
            materialsById.set(String(material.id), material);
            const barcode = normalizeBarcode(material.barcode);
            if (barcode) {{
                materialsByBarcode.set(barcode, material);
            }}
        }});

        appData.users.forEach((user) => {{
            usersById.set(String(user.id), user);
            const barcode = normalizeBarcode(user.barcode);
            if (barcode) {{
                usersByBarcode.set(barcode, user);
            }}
        }});

        appData.locations.forEach((location) => {{
            locationsById.set(String(location.id), location);
            const barcode = normalizeBarcode(location.barcode);
            if (barcode) {{
                locationsByBarcode.set(barcode, location);
            }}
        }});

        function setupMaterialPickers() {{
            document.querySelectorAll("[data-material-picker]").forEach((picker) => {{
                const categorySelect = picker.querySelector("[data-material-category]");
                const materialSelect = picker.querySelector("[data-material-select]");
                if (!categorySelect || !materialSelect) {{
                    return;
                }}

                const materialOptions = Array.from(materialSelect.options)
                    .filter((option) => option.value)
                    .map((option) => ({{
                        value: option.value,
                        text: option.textContent,
                        category: option.dataset.category || "",
                        unavailable: option.dataset.unavailable === "1",
                    }}));

                function selectedMaterialOption(value = materialSelect.value) {{
                    return materialOptions.find((option) => option.value === value);
                }}

                function syncCategoryFromMaterial() {{
                    const option = selectedMaterialOption();
                    const category = option?.category || "";
                    if (!category) {{
                        return false;
                    }}
                    if (categorySelect.value !== category) {{
                        categorySelect.value = category;
                        return true;
                    }}
                    return false;
                }}

                function applyCategoryFilter(keepSelection = true) {{
                    const selectedCategory = categorySelect.value;
                    const normalizedSelected = normalizeCategory(selectedCategory);
                    const pickerDisabled = materialSelect.dataset.pickerDisabled === "1";
                    const selectedValue = materialSelect.value;

                    materialSelect.replaceChildren(
                        new Option(selectedCategory ? "Material waehlen" : "Erst Kategorie waehlen", "")
                    );

                    materialOptions
                        .filter((option) => (
                            normalizedSelected
                            && normalizeCategory(option.category) === normalizedSelected
                        ))
                        .forEach((item) => {{
                            const option = new Option(item.text, item.value);
                            option.dataset.category = item.category;
                            if (item.unavailable) {{
                                option.dataset.unavailable = "1";
                                option.disabled = true;
                            }}
                            materialSelect.append(option);
                        }});

                    const option = selectedMaterialOption(selectedValue);
                    const canKeepSelection = (
                        keepSelection
                        && selectedCategory
                        && option
                        && !option.unavailable
                        && normalizeCategory(option.category) === normalizedSelected
                    );
                    if (canKeepSelection) {{
                        materialSelect.value = selectedValue;
                    }} else {{
                        materialSelect.value = "";
                    }}
                    materialSelect.disabled = pickerDisabled || !selectedCategory;
                }}

                categorySelect.addEventListener("change", () => applyCategoryFilter(false));
                materialSelect.addEventListener("change", () => {{
                    if (syncCategoryFromMaterial()) {{
                        applyCategoryFilter(true);
                    }}
                }});

                syncCategoryFromMaterial();
                applyCategoryFilter(true);
            }});
        }}

        setupMaterialPickers();

        function showPanel(id) {{
            panels.forEach((panel) => panel.classList.toggle("active", panel.id === id));
            buttons.forEach((button) => button.classList.toggle("active", button.dataset.panel === id));
            window.localStorage.setItem("warenwirtschaft-panel", id);
            if (id === "scanner") {{
                window.setTimeout(() => {{
                    document.querySelector("#scanner [data-scan-input]:not([disabled])")?.focus();
                }}, 0);
            }}
        }}

        buttons.forEach((button) => {{
            button.addEventListener("click", () => showPanel(button.dataset.panel));
        }});

        document.querySelectorAll("[data-auto-submit]").forEach((select) => {{
            select.addEventListener("change", () => {{
                select.form?.requestSubmit();
            }});
        }});

        const requestedPanel = new URLSearchParams(window.location.search).get("panel");
        const savedPanel = window.localStorage.getItem("warenwirtschaft-panel");
        const initialPanel = requestedPanel || savedPanel;
        if (initialPanel && document.getElementById(initialPanel)) {{
            showPanel(initialPanel);
        }}

        const cameraSelect = document.getElementById("cameraSelect");
        const startCameraButton = document.getElementById("startCameraButton");
        const stopCameraButton = document.getElementById("stopCameraButton");
        const scannerStatus = document.getElementById("scannerStatus");
        const video = document.getElementById("barcodeVideo");
        const materialEditDialog = document.getElementById("materialEditDialog");
        const materialEditForm = document.getElementById("materialEditForm");
        const materialEditTitle = document.getElementById("materialEditTitle");
        const materialEditMeta = document.getElementById("materialEditMeta");
        const closeMaterialEdit = document.getElementById("closeMaterialEdit");
        const manualLoanScanForm = document.getElementById("manualLoanScanForm");
        const manualLoanBarcode = document.getElementById("manualLoanBarcode");
        const batchScanForm = document.getElementById("batchScanForm");
        const batchScanAction = document.getElementById("batchScanAction");
        const batchScanInput = document.getElementById("batchScanInput");
        const batchScanCodes = document.getElementById("batchScanCodes");
        const batchScanLocation = document.getElementById("batchScanLocation");
        const batchScanStatus = document.getElementById("batchScanStatus");
        const batchScanList = document.getElementById("batchScanList");
        const batchScanClear = document.getElementById("batchScanClear");
        const batchScanSubmit = document.getElementById("batchScanSubmit");
        const scanActionDialog = document.getElementById("scanActionDialog");
        const closeScanAction = document.getElementById("closeScanAction");
        const scanActionBarcode = document.getElementById("scanActionBarcode");
        const scanActionMaterial = document.getElementById("scanActionMaterial");
        const scanActionStatus = document.getElementById("scanActionStatus");
        const scanActionLoan = document.getElementById("scanActionLoan");
        const scanActionBookIn = document.getElementById("scanActionBookIn");
        const scanActionBookOut = document.getElementById("scanActionBookOut");
        const scanBookingDialog = document.getElementById("scanBookingDialog");
        const scanBookingForm = document.getElementById("scanBookingForm");
        const closeScanBooking = document.getElementById("closeScanBooking");
        const scanBookingTitle = document.getElementById("scanBookingTitle");
        const scanBookingBarcode = document.getElementById("scanBookingBarcode");
        const scanBookingMaterial = document.getElementById("scanBookingMaterial");
        const scanBookingStatus = document.getElementById("scanBookingStatus");
        const scanBookingAction = document.getElementById("scanBookingAction");
        const scanBookingBarcodeInput = document.getElementById("scanBookingBarcodeInput");
        const scanBookingQuantity = document.getElementById("scanBookingQuantity");
        const scanBookingNote = document.getElementById("scanBookingNote");
        const scanBookingSubmit = document.getElementById("scanBookingSubmit");
        const loanWizard = document.getElementById("loanWizard");
        const closeLoanWizard = document.getElementById("closeLoanWizard");
        const loanWizardTitle = document.getElementById("loanWizardTitle");
        const loanWizardBarcode = document.getElementById("loanWizardBarcode");
        const loanWizardMaterial = document.getElementById("loanWizardMaterial");
        const loanWizardMaterialId = document.getElementById("loanWizardMaterialId");
        const loanWizardUserBarcode = document.getElementById("loanWizardUserBarcode");
        const loanWizardUser = document.getElementById("loanWizardUser");
        const loanWizardLocationBarcode = document.getElementById("loanWizardLocationBarcode");
        const loanWizardLocation = document.getElementById("loanWizardLocation");
        const loanWizardQuantity = document.getElementById("loanWizardQuantity");
        const loanWizardNote = document.getElementById("loanWizardNote");
        const loanWizardSubmit = document.getElementById("loanWizardSubmit");
        const wizardStatus = document.getElementById("wizardStatus");
        const wizardUserForm = document.getElementById("wizardUserForm");
        const wizardLocationForm = document.getElementById("wizardLocationForm");
        const detectorFormats = ["code_128", "code_39", "ean_13", "ean_8", "upc_a", "upc_e", "qr_code"];
        let cameraStream = null;
        let barcodeReader = null;
        let scanFrameId = 0;
        let isDetecting = false;
        let lastDetectedBarcode = "";
        let selectedScanMaterial = null;
        let selectedScanBarcode = "";
        let selectedWizardMaterial = null;
        let pendingScanUser = null;
        let pendingScanLocation = null;
        let batchUser = null;
        let batchLocation = null;
        const batchMaterials = new Map();

        function setScannerStatus(message) {{
            scannerStatus.textContent = message;
        }}

        function setScanActionStatus(message) {{
            scanActionStatus.textContent = message;
        }}

        function setScanBookingStatus(message) {{
            scanBookingStatus.textContent = message;
        }}

        function setWizardStatus(message) {{
            wizardStatus.textContent = message;
        }}

        function openDialog(dialog) {{
            if (typeof dialog?.showModal === "function") {{
                dialog.showModal();
            }} else {{
                dialog?.setAttribute("open", "");
            }}
        }}

        function closeDialog(dialog) {{
            if (typeof dialog?.close === "function") {{
                dialog.close();
            }} else {{
                dialog?.removeAttribute("open");
            }}
        }}

        function materialLine(material) {{
            return material.category + " | frei " + material.quantityAvailable + " von " + material.quantityTotal;
        }}

        function renderMaterialSummary(container, material) {{
            container.replaceChildren();

            const title = document.createElement("strong");
            title.textContent = material.name;
            container.append(title);

            const meta = document.createElement("span");
            meta.className = "subline";
            meta.textContent = materialLine(material);
            container.append(meta);

            const destination = document.createElement("span");
            destination.className = "subline";
            destination.textContent = "Besitzer: " + material.owner + " | Ziel: " + material.destination;
            container.append(destination);
        }}

        function setBatchStatus(message) {{
            if (batchScanStatus) {{
                batchScanStatus.textContent = message;
            }}
        }}

        function batchActionNeedsAvailability() {{
            return batchScanAction?.value === "out" || batchScanAction?.value === "loan";
        }}

        function batchActionNeedsUser() {{
            return batchScanAction?.value === "loan";
        }}

        function buildBatchCodes() {{
            if (!batchScanCodes) {{
                return;
            }}
            const codes = [];
            if (batchActionNeedsUser() && batchUser?.barcode) {{
                codes.push(batchUser.barcode);
            }}
            if (batchActionNeedsUser() && batchLocation?.barcode) {{
                codes.push(batchLocation.barcode);
            }}
            batchMaterials.forEach((entry) => {{
                for (let index = 0; index < entry.count; index += 1) {{
                    codes.push(entry.barcode || entry.material.barcode);
                }}
            }});
            batchScanCodes.value = codes.join("\\n");
        }}

        function batchAvailabilityProblem() {{
            if (!batchActionNeedsAvailability()) {{
                return "";
            }}
            for (const entry of batchMaterials.values()) {{
                const available = Number(entry.material.quantityAvailable) || 0;
                if (entry.count > available) {{
                    return entry.material.name + ": nur " + available + " frei verfuegbar.";
                }}
            }}
            return "";
        }}

        function canSubmitBatch() {{
            if (!batchMaterials.size) {{
                return false;
            }}
            if (batchAvailabilityProblem()) {{
                return false;
            }}
            if (batchActionNeedsUser()) {{
                return Boolean(batchUser && (batchLocation || batchScanLocation?.value));
            }}
            return true;
        }}

        function updateBatchScanUi() {{
            if (!batchScanForm) {{
                return;
            }}

            const needsUser = batchActionNeedsUser();
            const hasLocations = Boolean(
                batchScanLocation
                && Array.from(batchScanLocation.options).some((option) => option.value)
            );
            if (batchScanLocation) {{
                batchScanLocation.disabled = !needsUser || !hasLocations;
                batchScanLocation.required = needsUser;
                if (needsUser && batchLocation) {{
                    batchScanLocation.value = String(batchLocation.id);
                }}
            }}

            buildBatchCodes();
            batchScanList?.replaceChildren();

            if (needsUser && batchUser) {{
                const userRow = document.createElement("div");
                userRow.className = "scan-list-header";
                const title = document.createElement("strong");
                title.textContent = batchUser.name;
                const code = document.createElement("span");
                code.textContent = batchUser.barcode || "";
                userRow.append(title, code);
                batchScanList?.append(userRow);
            }}

            if (needsUser && batchLocation) {{
                const locationRow = document.createElement("div");
                locationRow.className = "scan-list-header";
                const title = document.createElement("strong");
                title.textContent = batchLocation.name;
                const code = document.createElement("span");
                code.textContent = batchLocation.barcode || "";
                locationRow.append(title, code);
                batchScanList?.append(locationRow);
            }}

            batchMaterials.forEach((entry) => {{
                const row = document.createElement("div");
                row.className = "scan-item";
                const title = document.createElement("strong");
                title.textContent = entry.material.name;
                const meta = document.createElement("span");
                meta.textContent = entry.count + " x | frei " + entry.material.quantityAvailable;
                row.append(title, meta);
                batchScanList?.append(row);
            }});

            if (!batchMaterials.size) {{
                const empty = document.createElement("div");
                empty.className = "scan-item";
                empty.textContent = "Keine Codes im Stapel.";
                batchScanList?.append(empty);
            }}

            const problem = batchAvailabilityProblem();
            if (problem) {{
                setBatchStatus(problem);
            }} else if (needsUser && !batchUser) {{
                setBatchStatus("Nutzer-Code fehlt.");
            }} else if (needsUser && !batchLocation && !batchScanLocation?.value) {{
                setBatchStatus("Ort-Code oder Ort-Auswahl fehlt.");
            }} else if (batchMaterials.size) {{
                const total = Array.from(batchMaterials.values()).reduce((sum, entry) => sum + entry.count, 0);
                setBatchStatus(total + " Materialscan(s) bereit.");
            }}

            if (batchScanSubmit) {{
                batchScanSubmit.disabled = !canSubmitBatch();
            }}
        }}

        function addBatchBarcode(rawValue) {{
            const barcode = normalizeBarcode(rawValue);
            if (!barcode) {{
                return false;
            }}

            const user = usersByBarcode.get(barcode);
            if (user) {{
                batchUser = user;
                setBatchStatus("Nutzer erkannt: " + user.name);
                updateBatchScanUi();
                return true;
            }}

            const location = locationsByBarcode.get(barcode);
            if (location) {{
                batchLocation = location;
                setBatchStatus("Ort erkannt: " + location.name);
                updateBatchScanUi();
                return true;
            }}

            const material = materialsByBarcode.get(barcode);
            if (material) {{
                const key = String(material.id);
                const existing = batchMaterials.get(key);
                if (existing) {{
                    existing.count += 1;
                }} else {{
                    batchMaterials.set(key, {{ material, barcode, count: 1 }});
                }}
                setBatchStatus("Material erkannt: " + material.name);
                updateBatchScanUi();
                return true;
            }}

            setBatchStatus("Code " + barcode + " nicht gefunden.");
            return false;
        }}

        function clearBatchScan() {{
            batchUser = null;
            batchLocation = null;
            batchMaterials.clear();
            setBatchStatus("Bereit.");
            updateBatchScanUi();
            batchScanInput?.focus();
        }}

        function setFormField(form, field, value) {{
            const input = form.elements.namedItem(field);
            if (input) {{
                input.value = value || "";
            }}
        }}

        function openMaterialEdit(material) {{
            if (!materialEditDialog || !materialEditForm) {{
                return;
            }}

            setFormField(materialEditForm, "material_id", String(material.id));
            setFormField(materialEditForm, "name", material.name);
            setFormField(materialEditForm, "category", material.category);
            setFormField(materialEditForm, "owner", material.owner);
            setFormField(materialEditForm, "destination", material.destination);
            materialEditTitle.textContent = "Artikel bearbeiten";
            materialEditMeta.textContent = "ID " + material.id + (material.barcode ? " | Barcode " + material.barcode : "");

            openDialog(materialEditDialog);

            window.setTimeout(() => {{
                materialEditForm.elements.namedItem("name")?.focus();
            }}, 0);
        }}

        function closeMaterialEditDialog() {{
            closeDialog(materialEditDialog);
        }}

{CODE128_CAMERA_FALLBACK_SCRIPT}

        async function initBarcodeReader() {{
            barcodeReader = await createCameraBarcodeReader(video, setScannerStatus, detectorFormats);
            return Boolean(barcodeReader);
        }}

        async function listCameras() {{
            if (cameraSelect.disabled || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {{
                return;
            }}

            const currentValue = cameraSelect.value;
            const devices = await navigator.mediaDevices.enumerateDevices();
            const videoDevices = devices.filter((device) => device.kind === "videoinput");
            cameraSelect.replaceChildren();

            if (!videoDevices.length) {{
                cameraSelect.append(new Option("Keine Kamera gefunden", ""));
                return;
            }}

            videoDevices.forEach((device, index) => {{
                const label = device.label || "Kamera " + (index + 1);
                cameraSelect.append(new Option(label, device.deviceId));
            }});

            if (currentValue && videoDevices.some((device) => device.deviceId === currentValue)) {{
                cameraSelect.value = currentValue;
            }}
        }}

        function stopCamera(updateStatus = true) {{
            if (scanFrameId) {{
                window.cancelAnimationFrame(scanFrameId);
                scanFrameId = 0;
            }}
            if (cameraStream) {{
                cameraStream.getTracks().forEach((track) => track.stop());
                cameraStream = null;
            }}
            video.srcObject = null;
            startCameraButton.disabled = cameraSelect.disabled;
            stopCameraButton.disabled = true;
            if (updateStatus) {{
                setScannerStatus("Kamera aus.");
            }}
        }}

        async function scanFrame() {{
            if (!cameraStream || !barcodeReader) {{
                return;
            }}

            if (!isDetecting && video.readyState >= 2) {{
                isDetecting = true;
                try {{
                    const barcodes = await barcodeReader.detect();
                    if (barcodes.length) {{
                        handleBarcode(barcodes[0], "camera");
                    }}
                }} catch (error) {{
                    setScannerStatus("Das Kamerabild konnte nicht gelesen werden.");
                }} finally {{
                    isDetecting = false;
                }}
            }}

            if (cameraStream) {{
                scanFrameId = window.requestAnimationFrame(scanFrame);
            }}
        }}

        async function startCamera() {{
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
                setScannerStatus("Dieser Browser gibt keine Kamera frei.");
                return;
            }}

            try {{
                const detectorReady = barcodeReader || await initBarcodeReader();
                if (!detectorReady) {{
                    startCameraButton.disabled = true;
                    return;
                }}

                stopCamera(false);
                const deviceId = cameraSelect.value;
                const constraints = {{
                    audio: false,
                    video: deviceId ? {{ deviceId: {{ exact: deviceId }} }} : true,
                }};
                cameraStream = await navigator.mediaDevices.getUserMedia(constraints);
                video.srcObject = cameraStream;
                await video.play();
                await listCameras();
                startCameraButton.disabled = true;
                stopCameraButton.disabled = false;
                lastDetectedBarcode = "";
                setScannerStatus("Kamera aktiv.");
                scanFrame();
            }} catch (error) {{
                stopCamera(false);
                setScannerStatus("Kamera konnte nicht gestartet werden.");
            }}
        }}

        function syncWizardAvailability() {{
            const available = selectedWizardMaterial ? Number(selectedWizardMaterial.quantityAvailable) : 0;
            const hasMaterial = Boolean(selectedWizardMaterial && selectedWizardMaterial.id);
            const hasUser = Boolean(loanWizardUser.value);
            const hasLocation = Boolean(loanWizardLocation.value);
            const canLoan = hasMaterial && hasUser && hasLocation && available > 0;

            const fieldsEnabled = hasMaterial && available > 0;
            loanWizardQuantity.disabled = !fieldsEnabled;
            loanWizardNote.disabled = !fieldsEnabled;
            loanWizardSubmit.disabled = !canLoan;
            if (hasMaterial && available > 0) {{
                loanWizardQuantity.max = String(available);
                if (Number(loanWizardQuantity.value) < 1 || Number(loanWizardQuantity.value) > available) {{
                    loanWizardQuantity.value = "1";
                }}
            }}
        }}

        function applyWizardUserCode(rawValue) {{
            const barcode = normalizeBarcode(rawValue);
            if (!barcode) {{
                return false;
            }}
            const user = usersByBarcode.get(barcode);
            if (!user) {{
                setWizardStatus("Nutzer-Code " + barcode + " nicht gefunden.");
                return false;
            }}
            loanWizardUser.value = String(user.id);
            pendingScanUser = user;
            setWizardStatus("Nutzer erkannt: " + user.name);
            syncWizardAvailability();
            return true;
        }}

        function applyWizardLocationCode(rawValue) {{
            const barcode = normalizeBarcode(rawValue);
            if (!barcode) {{
                return false;
            }}
            const location = locationsByBarcode.get(barcode);
            if (!location) {{
                setWizardStatus("Ort-Code " + barcode + " nicht gefunden.");
                return false;
            }}
            loanWizardLocation.value = String(location.id);
            pendingScanLocation = location;
            setWizardStatus("Ort erkannt: " + location.name);
            syncWizardAvailability();
            return true;
        }}

        function renderWizardMaterial(material, barcode) {{
            renderMaterialSummary(loanWizardMaterial, material);
            loanWizardBarcode.textContent = "Barcode " + (material.barcode || barcode);
        }}

        function openScanActionDialog(material, barcode) {{
            selectedScanMaterial = material;
            selectedScanBarcode = barcode || material.barcode || "";
            const available = Number(material.quantityAvailable) || 0;
            const consumable = isConsumableMaterial(material);

            scanActionBarcode.textContent = "Barcode " + selectedScanBarcode;
            renderMaterialSummary(scanActionMaterial, material);
            scanActionLoan.querySelector("strong").textContent = consumable ? "Ausgabe-Wizard" : "Verleih-Wizard";
            scanActionLoan.disabled = available < 1;
            scanActionBookOut.disabled = available < 1;
            setScanActionStatus(available > 0
                ? "Material erkannt. Waehle Verleih, Einbuchen oder Ausbuchen."
                : "Kein frei verfuegbarer Bestand. Einbuchen ist moeglich.");
            openDialog(scanActionDialog);
        }}

        function closeScanActionDialog() {{
            closeDialog(scanActionDialog);
        }}

        function closeScanBookingDialog() {{
            closeDialog(scanBookingDialog);
        }}

        function openSelectedLoanWizard() {{
            if (!selectedScanMaterial) {{
                return;
            }}
            closeScanActionDialog();
            openLoanWizard(selectedScanMaterial, selectedScanBarcode);
        }}

        function openScanBookingWizard(action) {{
            if (!selectedScanMaterial) {{
                return;
            }}

            const bookIn = action === "in";
            const available = Number(selectedScanMaterial.quantityAvailable) || 0;
            closeScanActionDialog();
            scanBookingForm.reset();
            scanBookingAction.value = action;
            scanBookingBarcodeInput.value = selectedScanBarcode;
            scanBookingTitle.textContent = bookIn ? "Material einbuchen" : "Material ausbuchen";
            scanBookingSubmit.textContent = bookIn ? "Einbuchen" : "Ausbuchen";
            scanBookingNote.placeholder = bookIn ? "optional" : "z.B. verbraucht, defekt";
            scanBookingBarcode.textContent = "Barcode " + selectedScanBarcode;
            renderMaterialSummary(scanBookingMaterial, selectedScanMaterial);
            scanBookingQuantity.disabled = false;
            scanBookingSubmit.disabled = false;
            scanBookingQuantity.removeAttribute("max");
            scanBookingQuantity.value = "1";

            if (bookIn) {{
                setScanBookingStatus("Bestand fuer dieses Material erhoehen.");
            }} else {{
                scanBookingQuantity.max = String(available);
                if (available < 1) {{
                    scanBookingQuantity.disabled = true;
                    scanBookingSubmit.disabled = true;
                    setScanBookingStatus("Dieses Material ist aktuell nicht frei verfuegbar.");
                }} else {{
                    setScanBookingStatus("Maximal " + available + " frei verfuegbare Einheit(en) ausbuchen.");
                }}
            }}

            openDialog(scanBookingDialog);
            window.setTimeout(() => scanBookingQuantity.focus(), 0);
        }}

        function openLoanWizard(material, barcode) {{
            const consumable = isConsumableMaterial(material);
            selectedWizardMaterial = material;
            loanWizardMaterialId.value = String(material.id);
            loanWizardTitle.textContent = consumable ? "Material ausgeben" : "Material verleihen";
            loanWizardSubmit.textContent = consumable ? "Ausgeben" : "Verleihen";
            if (loanWizardUserBarcode) {{
                loanWizardUserBarcode.value = "";
            }}
            if (loanWizardLocationBarcode) {{
                loanWizardLocationBarcode.value = "";
            }}
            if (pendingScanUser) {{
                loanWizardUser.value = String(pendingScanUser.id);
            }}
            if (pendingScanLocation) {{
                loanWizardLocation.value = String(pendingScanLocation.id);
            }}
            renderWizardMaterial(material, barcode);
            setWizardStatus(material.quantityAvailable > 0
                ? (pendingScanUser || pendingScanLocation
                    ? "Material, Nutzer oder Ort erkannt."
                    : (consumable ? "Verbrauchsmaterial erkannt." : "Material erkannt."))
                : "Dieses Material ist aktuell nicht frei verfuegbar.");
            syncWizardAvailability();

            openDialog(loanWizard);
            if (!pendingScanUser) {{
                window.setTimeout(() => loanWizardUserBarcode?.focus(), 0);
            }} else if (!pendingScanLocation) {{
                window.setTimeout(() => loanWizardLocationBarcode?.focus(), 0);
            }}
        }}

        function handleBarcode(rawValue, source) {{
            const barcode = normalizeBarcode(rawValue);
            if (!barcode) {{
                return false;
            }}
            if (source === "camera" && barcode === lastDetectedBarcode) {{
                return false;
            }}
            lastDetectedBarcode = barcode;

            const user = usersByBarcode.get(barcode);
            if (user) {{
                pendingScanUser = user;
                setScannerStatus("Nutzer erkannt: " + user.name);
                return true;
            }}

            const location = locationsByBarcode.get(barcode);
            if (location) {{
                pendingScanLocation = location;
                setScannerStatus("Ort erkannt: " + location.name);
                return true;
            }}

            const material = materialsByBarcode.get(barcode);
            if (!material) {{
                setScannerStatus("Barcode " + barcode + " nicht im Materialbestand gefunden.");
                return false;
            }}

            setScannerStatus("Erkannt: " + barcode + " - " + material.name);
            if (source === "camera") {{
                stopCamera(false);
            }}
            openScanActionDialog(material, barcode);
            return true;
        }}

        function closeWizard() {{
            closeDialog(loanWizard);
        }}

        function ensurePlaceholder(select, label) {{
            const firstOption = select.options[0];
            if (!firstOption || firstOption.value !== "" || firstOption.textContent.startsWith("Erst ")) {{
                select.replaceChildren(new Option(label, ""));
            }}
        }}

        function upsertOption(select, id, label, placeholder) {{
            ensurePlaceholder(select, placeholder);
            const value = String(id);
            let option = Array.from(select.options).find((item) => item.value === value);
            if (!option) {{
                option = new Option(label, value);
                select.append(option);
            }}
            option.textContent = label;
            select.value = value;
            select.disabled = false;
        }}

        async function submitInlineCreate(form, url, type) {{
            const button = form.querySelector("button[type='submit']");
            button.disabled = true;
            setWizardStatus("Speichern...");

            try {{
                const response = await fetch(url, {{
                    method: "POST",
                    headers: {{"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}},
                    body: new URLSearchParams(new FormData(form)),
                }});
                const payload = await response.json();
                if (!response.ok || !payload.ok) {{
                    throw new Error(payload.error || "Speichern fehlgeschlagen.");
                }}

                if (type === "user") {{
                    appData.users.push(payload.user);
                    usersById.set(String(payload.user.id), payload.user);
                    const barcode = normalizeBarcode(payload.user.barcode);
                    if (barcode) {{
                        usersByBarcode.set(barcode, payload.user);
                    }}
                    upsertOption(loanWizardUser, payload.user.id, payload.user.name, "Nutzer waehlen");
                    setWizardStatus("Nutzer " + payload.user.name + " wurde angelegt. Code: " + payload.user.barcode);
                }} else {{
                    appData.locations.push(payload.location);
                    locationsById.set(String(payload.location.id), payload.location);
                    const barcode = normalizeBarcode(payload.location.barcode);
                    if (barcode) {{
                        locationsByBarcode.set(barcode, payload.location);
                    }}
                    upsertOption(loanWizardLocation, payload.location.id, payload.location.name, "Ort / Baustelle waehlen");
                    setWizardStatus("Baustelle " + payload.location.name + " wurde angelegt. Code: " + payload.location.barcode);
                }}

                form.reset();
                const details = form.closest("details");
                if (details) {{
                    details.open = false;
                }}
                syncWizardAvailability();
            }} catch (error) {{
                setWizardStatus(error.message || "Speichern fehlgeschlagen.");
            }} finally {{
                button.disabled = false;
            }}
        }}

        startCameraButton?.addEventListener("click", startCamera);
        stopCameraButton?.addEventListener("click", () => stopCamera());
        cameraSelect?.addEventListener("change", () => {{
            if (cameraStream) {{
                startCamera();
            }}
        }});

        document.querySelectorAll("[data-edit-material]").forEach((button) => {{
            button.addEventListener("click", () => {{
                const material = materialsById.get(button.dataset.editMaterial);
                if (material) {{
                    openMaterialEdit(material);
                }}
            }});
        }});

        closeMaterialEdit?.addEventListener("click", closeMaterialEditDialog);
        materialEditDialog?.addEventListener("click", (event) => {{
            if (event.target === materialEditDialog) {{
                closeMaterialEditDialog();
            }}
        }});

        manualLoanScanForm?.addEventListener("submit", (event) => {{
            event.preventDefault();
            if (handleBarcode(manualLoanBarcode.value, "manual")) {{
                manualLoanBarcode.value = "";
            }}
        }});

        manualLoanBarcode?.addEventListener("keydown", (event) => {{
            if (event.key === "Enter") {{
                event.preventDefault();
                manualLoanScanForm?.requestSubmit();
            }}
        }});

        batchScanInput?.addEventListener("keydown", (event) => {{
            if (event.key === "Enter") {{
                event.preventDefault();
                if (addBatchBarcode(batchScanInput.value)) {{
                    batchScanInput.value = "";
                }}
            }}
        }});

        batchScanAction?.addEventListener("change", updateBatchScanUi);
        batchScanLocation?.addEventListener("change", () => {{
            if (!batchScanLocation.value || !batchLocation || String(batchLocation.id) !== batchScanLocation.value) {{
                batchLocation = null;
            }}
            updateBatchScanUi();
        }});
        batchScanClear?.addEventListener("click", clearBatchScan);
        batchScanForm?.addEventListener("submit", (event) => {{
            if (normalizeBarcode(batchScanInput?.value)) {{
                if (addBatchBarcode(batchScanInput.value)) {{
                    batchScanInput.value = "";
                }}
            }}
            buildBatchCodes();
            if (!canSubmitBatch()) {{
                event.preventDefault();
                updateBatchScanUi();
                batchScanInput?.focus();
            }}
        }});
        updateBatchScanUi();

        scanActionLoan?.addEventListener("click", openSelectedLoanWizard);
        scanActionBookIn?.addEventListener("click", () => openScanBookingWizard("in"));
        scanActionBookOut?.addEventListener("click", () => openScanBookingWizard("out"));
        closeScanAction?.addEventListener("click", closeScanActionDialog);
        scanActionDialog?.addEventListener("click", (event) => {{
            if (event.target === scanActionDialog) {{
                closeScanActionDialog();
            }}
        }});

        closeScanBooking?.addEventListener("click", closeScanBookingDialog);
        scanBookingDialog?.addEventListener("click", (event) => {{
            if (event.target === scanBookingDialog) {{
                closeScanBookingDialog();
            }}
        }});

        loanWizardUser?.addEventListener("change", syncWizardAvailability);
        loanWizardUserBarcode?.addEventListener("keydown", (event) => {{
            if (event.key === "Enter") {{
                event.preventDefault();
                if (applyWizardUserCode(loanWizardUserBarcode.value)) {{
                    loanWizardUserBarcode.value = "";
                }}
            }}
        }});
        loanWizardLocationBarcode?.addEventListener("keydown", (event) => {{
            if (event.key === "Enter") {{
                event.preventDefault();
                if (applyWizardLocationCode(loanWizardLocationBarcode.value)) {{
                    loanWizardLocationBarcode.value = "";
                }}
            }}
        }});
        loanWizardLocation?.addEventListener("change", syncWizardAvailability);
        loanWizardQuantity?.addEventListener("input", syncWizardAvailability);
        closeLoanWizard?.addEventListener("click", closeWizard);
        loanWizard?.addEventListener("click", (event) => {{
            if (event.target === loanWizard) {{
                closeWizard();
            }}
        }});

        wizardUserForm?.addEventListener("submit", (event) => {{
            event.preventDefault();
            submitInlineCreate(wizardUserForm, "/api/users", "user");
        }});

        wizardLocationForm?.addEventListener("submit", (event) => {{
            event.preventDefault();
            submitInlineCreate(wizardLocationForm, "/api/locations", "location");
        }});

        document.addEventListener("visibilitychange", () => {{
            if (document.hidden) {{
                stopCamera();
            }}
        }});

        if (!cameraSelect.disabled) {{
            initBarcodeReader()
                .then((ready) => {{
                    if (ready) {{
                        setScannerStatus("Scanner bereit.");
                        listCameras();
                    }} else {{
                        startCameraButton.disabled = true;
                    }}
                }})
                .catch(() => {{
                    setScannerStatus("Barcode-Erkennung konnte nicht geladen werden.");
                    startCameraButton.disabled = true;
                }});
        }}
    </script>
</body>
</html>"""


MOBILE_STYLE = """
        :root {
            --bg: #f7f7f4;
            --surface: #ffffff;
            --ink: #202124;
            --muted: #65716d;
            --line: #d9ddd6;
            --accent: #146c63;
            --accent-dark: #0e4f49;
            --gold: #d59b2d;
            --soft: #e8f2ef;
            --danger: #a13a32;
            --danger-soft: #fff0ed;
        }

        * {
            box-sizing: border-box;
        }

        html {
            background: var(--bg);
        }

        body {
            margin: 0;
            background: var(--bg);
            color: var(--ink);
            font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }

        .mobile-app-header {
            position: sticky;
            top: 0;
            z-index: 10;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 14px 14px 12px;
            border-bottom: 4px solid var(--gold);
            background: #1f2d2a;
            color: #fff;
        }

        .mobile-app-header h1 {
            margin: 0;
            font-size: 22px;
            letter-spacing: 0;
            line-height: 1.05;
        }

        .mobile-app-header span {
            display: block;
            color: #dce7e4;
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
        }

        .header-link,
        .mobile-link-button,
        .mini-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 40px;
            border-radius: 8px;
            border: 1px solid transparent;
            background: var(--accent);
            color: #fff;
            font-weight: 750;
            padding: 8px 11px;
            text-decoration: none;
        }

        .header-link {
            min-height: 36px;
            background: #fff;
            color: #1f2d2a;
            white-space: nowrap;
        }

        .mobile-main {
            width: min(100% - 20px, 720px);
            margin: 14px auto 92px;
        }

        .flash {
            padding: 12px 13px;
            margin-bottom: 12px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--soft);
            font-weight: 700;
        }

        .flash.error {
            background: var(--danger-soft);
            border-color: #e2aaa2;
            color: var(--danger);
        }

        .mobile-stats {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
            margin-bottom: 14px;
        }

        .mobile-stat {
            min-height: 74px;
            padding: 11px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
        }

        .mobile-stat span {
            display: block;
            color: var(--muted);
            font-size: 12px;
            font-weight: 750;
            text-transform: uppercase;
        }

        .mobile-stat strong {
            display: block;
            margin-top: 4px;
            font-size: 28px;
            line-height: 1;
            font-variant-numeric: tabular-nums;
        }

        .mobile-panel {
            display: none;
        }

        .mobile-panel.active {
            display: grid;
            gap: 12px;
        }

        .mobile-section {
            display: grid;
            gap: 12px;
            margin-bottom: 14px;
        }

        .mobile-section h2 {
            margin: 0;
            font-size: 19px;
            letter-spacing: 0;
        }

        .mobile-section h3 {
            margin: 0;
            font-size: 16px;
            letter-spacing: 0;
        }

        .mobile-card {
            display: grid;
            gap: 10px;
            padding: 13px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
        }

        .card-top,
        .card-row,
        .card-actions,
        .button-row {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .card-top {
            justify-content: space-between;
            align-items: flex-start;
        }

        .card-row,
        .card-actions,
        .button-row {
            flex-wrap: wrap;
        }

        .card-top strong {
            display: block;
            font-size: 16px;
        }

        .subline,
        .muted {
            color: var(--muted);
            font-size: 13px;
        }

        .pill {
            flex: 0 0 auto;
            border-radius: 999px;
            padding: 4px 8px;
            background: var(--soft);
            color: var(--accent-dark);
            font-size: 12px;
            font-weight: 800;
            white-space: nowrap;
        }

        .pill.empty-stock {
            background: var(--danger-soft);
            color: var(--danger);
        }

        .mobile-form {
            display: grid;
            gap: 10px;
            padding: 12px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
        }

        .material-picker {
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
        }

        .inline-form {
            margin: 0;
        }

        label {
            display: grid;
            gap: 5px;
            color: var(--muted);
            font-size: 13px;
            font-weight: 750;
        }

        input,
        select,
        textarea {
            width: 100%;
            min-height: 44px;
            border: 1px solid var(--line);
            border-radius: 7px;
            background: #fff;
            color: var(--ink);
            font: inherit;
            padding: 10px 11px;
        }

        textarea {
            min-height: 82px;
            resize: vertical;
        }

        button,
        .mobile-button {
            min-height: 44px;
            border: 0;
            border-radius: 8px;
            background: var(--accent);
            color: #fff;
            cursor: pointer;
            font: inherit;
            font-weight: 800;
            padding: 10px 12px;
        }

        button:hover,
        .mobile-link-button:hover {
            background: var(--accent-dark);
        }

        button:disabled {
            background: #9da8a5;
            cursor: not-allowed;
        }

        .secondary-button {
            border: 1px solid var(--line);
            background: #eef1ed;
            color: var(--ink);
        }

        .secondary-button:hover {
            background: #dde4df;
        }

        .mini-button,
        .mini-link {
            min-height: 36px;
            padding: 7px 10px;
            font-size: 13px;
        }

        .danger-button {
            background: var(--danger);
        }

        .scan-input {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 18px;
            letter-spacing: 0;
        }

        .search-input {
            margin-bottom: 4px;
        }

        .mobile-list {
            display: grid;
            gap: 9px;
        }

        .empty-card {
            color: var(--muted);
        }

        .scanner-video {
            width: 100%;
            aspect-ratio: 4 / 3;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #121715;
            object-fit: cover;
        }

        .scanner-status {
            min-height: 42px;
            padding: 10px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #fff;
            color: var(--muted);
        }

        .scan-list {
            display: grid;
            gap: 8px;
        }

        .scan-list-header,
        .scan-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            padding: 9px 10px;
            border: 1px solid var(--line);
            border-radius: 7px;
            background: #fff;
        }

        .scan-list-header {
            background: var(--soft);
            color: var(--accent-dark);
            font-weight: 800;
        }

        .scan-item span {
            color: var(--muted);
            font-size: 13px;
        }

        .mobile-dialog {
            width: min(100% - 20px, 540px);
            border: 0;
            border-radius: 8px;
            padding: 0;
            box-shadow: 0 24px 70px rgba(20, 28, 26, 0.32);
        }

        .mobile-dialog::backdrop {
            background: rgba(20, 28, 26, 0.48);
        }

        .dialog-shell {
            display: grid;
            gap: 12px;
            padding: 14px;
            background: #fff;
        }

        .dialog-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 10px;
        }

        .dialog-header h2 {
            margin: 0;
            font-size: 20px;
            letter-spacing: 0;
        }

        .icon-button {
            width: 38px;
            height: 38px;
            min-height: 38px;
            padding: 0;
            display: inline-grid;
            place-items: center;
            border: 1px solid var(--line);
            background: #eef1ed;
            color: var(--ink);
            font-size: 22px;
            line-height: 1;
        }

        .material-summary {
            display: grid;
            gap: 4px;
            padding: 11px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--soft);
        }

        .action-choice-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
        }

        .action-choice-button {
            min-height: 72px;
            text-align: left;
            background: #fff;
            border: 1px solid var(--line);
            color: var(--ink);
        }

        .action-choice-button span {
            display: block;
            margin-top: 2px;
            color: var(--muted);
            font-size: 13px;
            font-weight: 500;
        }

        .mobile-bottom-tabs {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            z-index: 12;
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 0;
            border-top: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.96);
            backdrop-filter: blur(12px);
        }

        .mobile-tab-button {
            min-height: 62px;
            border-radius: 0;
            border-right: 1px solid var(--line);
            background: transparent;
            color: var(--muted);
            font-size: 12px;
            font-weight: 850;
            line-height: 1.1;
            overflow-wrap: anywhere;
            padding: 8px 4px;
        }

        .mobile-tab-button:last-child {
            border-right: 0;
        }

        .mobile-tab-button.active {
            background: var(--soft);
            color: var(--accent-dark);
        }

        @media (min-width: 640px) {
            .mobile-stats {
                grid-template-columns: repeat(4, minmax(0, 1fr));
            }

            .action-choice-grid {
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }
        }
"""


MOBILE_SCRIPT = CODE128_CAMERA_FALLBACK_SCRIPT + """
        (() => {
            const appData = window.mobileAppData || { materials: [], users: [], locations: [] };
            const buttons = Array.from(document.querySelectorAll("[data-mobile-panel-button]"));
            const panels = Array.from(document.querySelectorAll(".mobile-panel"));
            const materialsByBarcode = new Map();
            const materialsById = new Map();
            const usersByBarcode = new Map();
            const usersById = new Map();
            const locationsByBarcode = new Map();
            const locationsById = new Map();
            const consumableCategory = "verbrauchsmaterial";
            let selectedScanMaterial = null;
            let selectedScanBarcode = "";
            let batchUser = null;
            let batchLocation = null;
            const batchMaterials = new Map();
            let cameraStream = null;
            let barcodeReader = null;
            let scanFrameId = 0;
            let isDetecting = false;
            let lastDetectedBarcode = "";

            function normalizeBarcode(value) {
                return String(value || "").trim().toUpperCase().replace(/\\s+/g, "");
            }

            function normalizeCategory(value) {
                return String(value || "").trim().toLocaleLowerCase("de-DE");
            }

            function isConsumable(material) {
                return normalizeCategory(material?.category) === consumableCategory;
            }

            appData.materials.forEach((material) => {
                materialsById.set(String(material.id), material);
                const barcode = normalizeBarcode(material.barcode);
                if (barcode) {
                    materialsByBarcode.set(barcode, material);
                }
            });

            appData.users.forEach((user) => {
                usersById.set(String(user.id), user);
                const barcode = normalizeBarcode(user.barcode);
                if (barcode) {
                    usersByBarcode.set(barcode, user);
                }
            });

            appData.locations.forEach((location) => {
                locationsById.set(String(location.id), location);
                const barcode = normalizeBarcode(location.barcode);
                if (barcode) {
                    locationsByBarcode.set(barcode, location);
                }
            });

            function setupMaterialPickers() {
                document.querySelectorAll("[data-material-picker]").forEach((picker) => {
                    const categorySelect = picker.querySelector("[data-material-category]");
                    const materialSelect = picker.querySelector("[data-material-select]");
                    if (!categorySelect || !materialSelect) {
                        return;
                    }

                    const materialOptions = Array.from(materialSelect.options)
                        .filter((option) => option.value)
                        .map((option) => ({
                            value: option.value,
                            text: option.textContent,
                            category: option.dataset.category || "",
                            unavailable: option.dataset.unavailable === "1",
                        }));

                    function selectedMaterialOption(value = materialSelect.value) {
                        return materialOptions.find((option) => option.value === value);
                    }

                    function syncCategoryFromMaterial() {
                        const option = selectedMaterialOption();
                        const category = option?.category || "";
                        if (!category) {
                            return false;
                        }
                        if (categorySelect.value !== category) {
                            categorySelect.value = category;
                            return true;
                        }
                        return false;
                    }

                    function applyCategoryFilter(keepSelection = true) {
                        const selectedCategory = categorySelect.value;
                        const normalizedSelected = normalizeCategory(selectedCategory);
                        const pickerDisabled = materialSelect.dataset.pickerDisabled === "1";
                        const selectedValue = materialSelect.value;

                        materialSelect.replaceChildren(
                            new Option(selectedCategory ? "Material waehlen" : "Erst Kategorie waehlen", "")
                        );

                        materialOptions
                            .filter((option) => (
                                normalizedSelected
                                && normalizeCategory(option.category) === normalizedSelected
                            ))
                            .forEach((item) => {
                                const option = new Option(item.text, item.value);
                                option.dataset.category = item.category;
                                if (item.unavailable) {
                                    option.dataset.unavailable = "1";
                                    option.disabled = true;
                                }
                                materialSelect.append(option);
                            });

                        const option = selectedMaterialOption(selectedValue);
                        const canKeepSelection = (
                            keepSelection
                            && selectedCategory
                            && option
                            && !option.unavailable
                            && normalizeCategory(option.category) === normalizedSelected
                        );
                        if (canKeepSelection) {
                            materialSelect.value = selectedValue;
                        } else {
                            materialSelect.value = "";
                        }
                        materialSelect.disabled = pickerDisabled || !selectedCategory;
                    }

                    categorySelect.addEventListener("change", () => applyCategoryFilter(false));
                    materialSelect.addEventListener("change", () => {
                        if (syncCategoryFromMaterial()) {
                            applyCategoryFilter(true);
                        }
                    });

                    syncCategoryFromMaterial();
                    applyCategoryFilter(true);
                });
            }

            setupMaterialPickers();

            function showPanel(id) {
                panels.forEach((panel) => panel.classList.toggle("active", panel.id === id));
                buttons.forEach((button) => {
                    button.classList.toggle("active", button.dataset.mobilePanelButton === id);
                });
                window.localStorage.setItem("warenwirtschaft-mobile-panel", id);
                if (id === "mobile-scanner") {
                    window.setTimeout(() => document.getElementById("mobileScanBarcode")?.focus(), 0);
                }
            }

            buttons.forEach((button) => {
                button.addEventListener("click", () => showPanel(button.dataset.mobilePanelButton));
            });

            const requestedPanel = new URLSearchParams(window.location.search).get("panel");
            const savedPanel = window.localStorage.getItem("warenwirtschaft-mobile-panel");
            const initialPanel = requestedPanel ? "mobile-" + requestedPanel : savedPanel;
            if (initialPanel && document.getElementById(initialPanel)) {
                showPanel(initialPanel);
            }

            const search = document.getElementById("mobileSearch");
            const materialCards = Array.from(document.querySelectorAll("[data-mobile-material-card]"));
            search?.addEventListener("input", () => {
                const needle = search.value.trim().toLocaleLowerCase("de-DE");
                materialCards.forEach((card) => {
                    const haystack = card.dataset.search || "";
                    card.hidden = Boolean(needle && !haystack.includes(needle));
                });
            });

            function setText(id, text) {
                const element = document.getElementById(id);
                if (element) {
                    element.textContent = text;
                }
            }

            function openDialog(dialog) {
                if (typeof dialog?.showModal === "function") {
                    dialog.showModal();
                } else {
                    dialog?.setAttribute("open", "");
                }
            }

            function closeDialog(dialog) {
                if (typeof dialog?.close === "function") {
                    dialog.close();
                } else {
                    dialog?.removeAttribute("open");
                }
            }

            function setFormField(form, field, value) {
                const input = form?.elements.namedItem(field);
                if (input) {
                    input.value = value || "";
                }
            }

            function materialLine(material) {
                return material.category + " | frei " + material.quantityAvailable + " von " + material.quantityTotal;
            }

            function renderMaterialSummary(container, material) {
                container.replaceChildren();

                const title = document.createElement("strong");
                title.textContent = material.name;
                container.append(title);

                const meta = document.createElement("span");
                meta.className = "subline";
                meta.textContent = materialLine(material);
                container.append(meta);

                const destination = document.createElement("span");
                destination.className = "subline";
                destination.textContent = "Besitzer: " + material.owner + " | Ziel: " + material.destination;
                container.append(destination);
            }

            const materialEditDialog = document.getElementById("mobileMaterialEditDialog");
            const materialEditForm = document.getElementById("mobileMaterialEditForm");
            const materialEditMeta = document.getElementById("mobileMaterialEditMeta");

            document.querySelectorAll("[data-mobile-edit]").forEach((button) => {
                button.addEventListener("click", () => {
                    const material = materialsById.get(button.dataset.mobileEdit);
                    if (!material) {
                        return;
                    }
                    setFormField(materialEditForm, "material_id", String(material.id));
                    setFormField(materialEditForm, "name", material.name);
                    setFormField(materialEditForm, "category", material.category);
                    setFormField(materialEditForm, "owner", material.owner);
                    setFormField(materialEditForm, "destination", material.destination);
                    materialEditMeta.textContent = "ID " + material.id + (material.barcode ? " | " + material.barcode : "");
                    openDialog(materialEditDialog);
                    window.setTimeout(() => materialEditForm.elements.namedItem("name")?.focus(), 0);
                });
            });

            document.querySelectorAll("[data-close-dialog]").forEach((button) => {
                button.addEventListener("click", () => closeDialog(button.closest("dialog")));
            });

            document.querySelectorAll("dialog").forEach((dialog) => {
                dialog.addEventListener("click", (event) => {
                    if (event.target === dialog) {
                        closeDialog(dialog);
                    }
                });
            });

            const scanForm = document.getElementById("mobileScanForm");
            const scanInput = document.getElementById("mobileScanBarcode");
            const scanDialog = document.getElementById("mobileScanDialog");
            const scanSummary = document.getElementById("mobileScanSummary");
            const scanActionLoan = document.getElementById("mobileScanActionLoan");
            const scanActionIn = document.getElementById("mobileScanActionIn");
            const scanActionOut = document.getElementById("mobileScanActionOut");
            const scanBookingDialog = document.getElementById("mobileScanBookingDialog");
            const scanBookingForm = document.getElementById("mobileScanBookingForm");
            const scanBookingTitle = document.getElementById("mobileScanBookingTitle");
            const scanBookingSummary = document.getElementById("mobileScanBookingSummary");
            const scanBookingAction = document.getElementById("mobileScanBookingAction");
            const scanBookingBarcode = document.getElementById("mobileScanBookingBarcode");
            const scanBookingQuantity = document.getElementById("mobileScanBookingQuantity");
            const scanBookingNote = document.getElementById("mobileScanBookingNote");
            const scanBookingSubmit = document.getElementById("mobileScanBookingSubmit");
            const loanMaterialSelect = document.getElementById("mobileLoanMaterial");
            const loanUserSelect = document.getElementById("mobileLoanUser");
            const loanUserBarcode = document.getElementById("mobileLoanUserBarcode");
            const loanLocationSelect = document.getElementById("mobileLoanLocation");
            const loanLocationBarcode = document.getElementById("mobileLoanLocationBarcode");
            const batchScanForm = document.getElementById("mobileBatchScanForm");
            const batchScanAction = document.getElementById("mobileBatchScanAction");
            const batchScanInput = document.getElementById("mobileBatchScanInput");
            const batchScanCodes = document.getElementById("mobileBatchScanCodes");
            const batchScanLocation = document.getElementById("mobileBatchScanLocation");
            const batchScanStatus = document.getElementById("mobileBatchScanStatus");
            const batchScanList = document.getElementById("mobileBatchScanList");
            const batchScanClear = document.getElementById("mobileBatchScanClear");
            const batchScanSubmit = document.getElementById("mobileBatchScanSubmit");

            function setBatchStatus(message) {
                if (batchScanStatus) {
                    batchScanStatus.textContent = message;
                }
            }

            function batchActionNeedsAvailability() {
                return batchScanAction?.value === "out" || batchScanAction?.value === "loan";
            }

            function batchActionNeedsUser() {
                return batchScanAction?.value === "loan";
            }

            function buildBatchCodes() {
                if (!batchScanCodes) {
                    return;
                }
                const codes = [];
                if (batchActionNeedsUser() && batchUser?.barcode) {
                    codes.push(batchUser.barcode);
                }
                if (batchActionNeedsUser() && batchLocation?.barcode) {
                    codes.push(batchLocation.barcode);
                }
                batchMaterials.forEach((entry) => {
                    for (let index = 0; index < entry.count; index += 1) {
                        codes.push(entry.barcode || entry.material.barcode);
                    }
                });
                batchScanCodes.value = codes.join("\\n");
            }

            function batchAvailabilityProblem() {
                if (!batchActionNeedsAvailability()) {
                    return "";
                }
                for (const entry of batchMaterials.values()) {
                    const available = Number(entry.material.quantityAvailable) || 0;
                    if (entry.count > available) {
                        return entry.material.name + ": nur " + available + " frei verfuegbar.";
                    }
                }
                return "";
            }

            function canSubmitBatch() {
                if (!batchMaterials.size) {
                    return false;
                }
                if (batchAvailabilityProblem()) {
                    return false;
                }
                if (batchActionNeedsUser()) {
                    return Boolean(batchUser && (batchLocation || batchScanLocation?.value));
                }
                return true;
            }

            function updateBatchScanUi() {
                if (!batchScanForm) {
                    return;
                }

                const needsUser = batchActionNeedsUser();
                const hasLocations = Boolean(
                    batchScanLocation
                    && Array.from(batchScanLocation.options).some((option) => option.value)
                );
                if (batchScanLocation) {
                    batchScanLocation.disabled = !needsUser || !hasLocations;
                    batchScanLocation.required = needsUser;
                    if (needsUser && batchLocation) {
                        batchScanLocation.value = String(batchLocation.id);
                    }
                }

                buildBatchCodes();
                batchScanList?.replaceChildren();

                if (needsUser && batchUser) {
                    const userRow = document.createElement("div");
                    userRow.className = "scan-list-header";
                    const title = document.createElement("strong");
                    title.textContent = batchUser.name;
                    const code = document.createElement("span");
                    code.textContent = batchUser.barcode || "";
                    userRow.append(title, code);
                    batchScanList?.append(userRow);
                }

                if (needsUser && batchLocation) {
                    const locationRow = document.createElement("div");
                    locationRow.className = "scan-list-header";
                    const title = document.createElement("strong");
                    title.textContent = batchLocation.name;
                    const code = document.createElement("span");
                    code.textContent = batchLocation.barcode || "";
                    locationRow.append(title, code);
                    batchScanList?.append(locationRow);
                }

                batchMaterials.forEach((entry) => {
                    const row = document.createElement("div");
                    row.className = "scan-item";
                    const title = document.createElement("strong");
                    title.textContent = entry.material.name;
                    const meta = document.createElement("span");
                    meta.textContent = entry.count + " x | frei " + entry.material.quantityAvailable;
                    row.append(title, meta);
                    batchScanList?.append(row);
                });

                if (!batchMaterials.size) {
                    const empty = document.createElement("div");
                    empty.className = "scan-item";
                    empty.textContent = "Keine Codes im Stapel.";
                    batchScanList?.append(empty);
                }

                const problem = batchAvailabilityProblem();
                if (problem) {
                    setBatchStatus(problem);
                } else if (needsUser && !batchUser) {
                    setBatchStatus("Nutzer-Code fehlt.");
                } else if (needsUser && !batchLocation && !batchScanLocation?.value) {
                    setBatchStatus("Ort-Code oder Ort-Auswahl fehlt.");
                } else if (batchMaterials.size) {
                    const total = Array.from(batchMaterials.values()).reduce((sum, entry) => sum + entry.count, 0);
                    setBatchStatus(total + " Materialscan(s) bereit.");
                }

                if (batchScanSubmit) {
                    batchScanSubmit.disabled = !canSubmitBatch();
                }
            }

            function addBatchBarcode(rawValue) {
                const barcode = normalizeBarcode(rawValue);
                if (!barcode) {
                    return false;
                }

                const user = usersByBarcode.get(barcode);
                if (user) {
                    batchUser = user;
                    setBatchStatus("Nutzer erkannt: " + user.name);
                    updateBatchScanUi();
                    return true;
                }

                const location = locationsByBarcode.get(barcode);
                if (location) {
                    batchLocation = location;
                    setBatchStatus("Ort erkannt: " + location.name);
                    updateBatchScanUi();
                    return true;
                }

                const material = materialsByBarcode.get(barcode);
                if (material) {
                    const key = String(material.id);
                    const existing = batchMaterials.get(key);
                    if (existing) {
                        existing.count += 1;
                    } else {
                        batchMaterials.set(key, { material, barcode, count: 1 });
                    }
                    setBatchStatus("Material erkannt: " + material.name);
                    updateBatchScanUi();
                    return true;
                }

                setBatchStatus("Code " + barcode + " nicht gefunden.");
                return false;
            }

            function clearBatchScan() {
                batchUser = null;
                batchLocation = null;
                batchMaterials.clear();
                setBatchStatus("Bereit.");
                updateBatchScanUi();
                batchScanInput?.focus();
            }

            function applyMobileLoanUserCode(rawValue) {
                const barcode = normalizeBarcode(rawValue);
                if (!barcode) {
                    return false;
                }
                const user = usersByBarcode.get(barcode);
                if (!user) {
                    setText("mobileScannerStatus", "Nutzer-Code " + barcode + " nicht gefunden.");
                    return false;
                }
                if (loanUserSelect) {
                    loanUserSelect.value = String(user.id);
                }
                setText("mobileScannerStatus", "Nutzer erkannt: " + user.name);
                return true;
            }

            function applyMobileLoanLocationCode(rawValue) {
                const barcode = normalizeBarcode(rawValue);
                if (!barcode) {
                    return false;
                }
                const location = locationsByBarcode.get(barcode);
                if (!location) {
                    setText("mobileScannerStatus", "Ort-Code " + barcode + " nicht gefunden.");
                    return false;
                }
                if (loanLocationSelect) {
                    loanLocationSelect.value = String(location.id);
                }
                setText("mobileScannerStatus", "Ort erkannt: " + location.name);
                return true;
            }

            function openScanAction(material, barcode) {
                selectedScanMaterial = material;
                selectedScanBarcode = barcode || material.barcode || "";
                const available = Number(material.quantityAvailable) || 0;
                renderMaterialSummary(scanSummary, material);
                setText("mobileScanDialogBarcode", "Barcode " + selectedScanBarcode);
                scanActionLoan.querySelector("strong").textContent = isConsumable(material)
                    ? "Ausgabe"
                    : "Verleih";
                scanActionLoan.disabled = available < 1;
                scanActionOut.disabled = available < 1;
                openDialog(scanDialog);
            }

            function handleBarcode(rawValue, source) {
                const barcode = normalizeBarcode(rawValue);
                if (!barcode) {
                    return false;
                }
                if (source === "camera" && barcode === lastDetectedBarcode) {
                    return false;
                }
                lastDetectedBarcode = barcode;

                const user = usersByBarcode.get(barcode);
                if (user) {
                    applyMobileLoanUserCode(barcode);
                    return true;
                }

                const location = locationsByBarcode.get(barcode);
                if (location) {
                    applyMobileLoanLocationCode(barcode);
                    return true;
                }

                const material = materialsByBarcode.get(barcode);
                if (!material) {
                    setText("mobileScannerStatus", "Barcode " + barcode + " nicht gefunden.");
                    return false;
                }
                setText("mobileScannerStatus", "Erkannt: " + material.name);
                if (source === "camera") {
                    stopCamera(false);
                }
                openScanAction(material, barcode);
                return true;
            }

            scanForm?.addEventListener("submit", (event) => {
                event.preventDefault();
                if (handleBarcode(scanInput.value, "manual")) {
                    scanInput.value = "";
                }
            });

            batchScanInput?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    if (addBatchBarcode(batchScanInput.value)) {
                        batchScanInput.value = "";
                    }
                }
            });

            batchScanAction?.addEventListener("change", updateBatchScanUi);
            batchScanLocation?.addEventListener("change", () => {
                if (!batchScanLocation.value || !batchLocation || String(batchLocation.id) !== batchScanLocation.value) {
                    batchLocation = null;
                }
                updateBatchScanUi();
            });
            batchScanClear?.addEventListener("click", clearBatchScan);
            batchScanForm?.addEventListener("submit", (event) => {
                if (normalizeBarcode(batchScanInput?.value)) {
                    if (addBatchBarcode(batchScanInput.value)) {
                        batchScanInput.value = "";
                    }
                }
                buildBatchCodes();
                if (!canSubmitBatch()) {
                    event.preventDefault();
                    updateBatchScanUi();
                    batchScanInput?.focus();
                }
            });
            updateBatchScanUi();

            loanUserBarcode?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    if (applyMobileLoanUserCode(loanUserBarcode.value)) {
                        loanUserBarcode.value = "";
                    }
                }
            });

            loanLocationBarcode?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    if (applyMobileLoanLocationCode(loanLocationBarcode.value)) {
                        loanLocationBarcode.value = "";
                    }
                }
            });

            scanActionLoan?.addEventListener("click", () => {
                if (!selectedScanMaterial) {
                    return;
                }
                closeDialog(scanDialog);
                showPanel("mobile-loan");
                if (loanMaterialSelect) {
                    loanMaterialSelect.value = String(selectedScanMaterial.id);
                    loanMaterialSelect.dispatchEvent(new Event("change", { bubbles: true }));
                    loanMaterialSelect.scrollIntoView({ behavior: "smooth", block: "center" });
                }
            });

            function openScanBooking(action) {
                if (!selectedScanMaterial) {
                    return;
                }
                const bookIn = action === "in";
                const available = Number(selectedScanMaterial.quantityAvailable) || 0;
                closeDialog(scanDialog);
                scanBookingForm.reset();
                scanBookingAction.value = action;
                scanBookingBarcode.value = selectedScanBarcode;
                scanBookingTitle.textContent = bookIn ? "Material einbuchen" : "Material ausbuchen";
                scanBookingSubmit.textContent = bookIn ? "Einbuchen" : "Ausbuchen";
                scanBookingNote.placeholder = bookIn ? "optional" : "z.B. verbraucht, defekt";
                scanBookingQuantity.disabled = false;
                scanBookingSubmit.disabled = false;
                scanBookingQuantity.removeAttribute("max");
                scanBookingQuantity.value = "1";
                renderMaterialSummary(scanBookingSummary, selectedScanMaterial);

                if (!bookIn) {
                    scanBookingQuantity.max = String(available);
                    if (available < 1) {
                        scanBookingQuantity.disabled = true;
                        scanBookingSubmit.disabled = true;
                    }
                }

                openDialog(scanBookingDialog);
                window.setTimeout(() => scanBookingQuantity.focus(), 0);
            }

            scanActionIn?.addEventListener("click", () => openScanBooking("in"));
            scanActionOut?.addEventListener("click", () => openScanBooking("out"));

            const video = document.getElementById("mobileBarcodeVideo");
            const startCameraButton = document.getElementById("mobileStartCamera");
            const stopCameraButton = document.getElementById("mobileStopCamera");

            function stopCamera(updateStatus = true) {
                if (scanFrameId) {
                    window.cancelAnimationFrame(scanFrameId);
                    scanFrameId = 0;
                }
                if (cameraStream) {
                    cameraStream.getTracks().forEach((track) => track.stop());
                    cameraStream = null;
                }
                if (video) {
                    video.srcObject = null;
                }
                if (startCameraButton) {
                    startCameraButton.disabled = false;
                }
                if (stopCameraButton) {
                    stopCameraButton.disabled = true;
                }
                if (updateStatus) {
                    setText("mobileScannerStatus", "Kamera aus.");
                }
            }

            async function prepareBarcodeReader() {
                barcodeReader = await createCameraBarcodeReader(
                    video,
                    (message) => setText("mobileScannerStatus", message),
                    ["code_128", "code_39", "ean_13", "ean_8", "qr_code"],
                );
                return Boolean(barcodeReader);
            }

            async function scanFrame() {
                if (!cameraStream || !barcodeReader) {
                    return;
                }

                if (!isDetecting && video?.readyState >= 2) {
                    isDetecting = true;
                    try {
                        const barcodes = await barcodeReader.detect();
                        if (barcodes.length) {
                            handleBarcode(barcodes[0], "camera");
                        }
                    } catch (error) {
                        setText("mobileScannerStatus", "Das Kamerabild konnte nicht gelesen werden.");
                    } finally {
                        isDetecting = false;
                    }
                }

                if (cameraStream) {
                    scanFrameId = window.requestAnimationFrame(scanFrame);
                }
            }

            async function startCamera() {
                if (!window.isSecureContext) {
                    setText("mobileScannerStatus", "Kamera braucht HTTPS oder localhost. Barcode-Feld bleibt nutzbar.");
                    return;
                }
                if (!navigator.mediaDevices?.getUserMedia) {
                    setText("mobileScannerStatus", "Dieser Browser gibt keine Kamera frei.");
                    return;
                }

                try {
                    const ready = barcodeReader || await prepareBarcodeReader();
                    if (!ready) {
                        return;
                    }
                    stopCamera(false);
                    cameraStream = await navigator.mediaDevices.getUserMedia({
                        audio: false,
                        video: { facingMode: { ideal: "environment" } },
                    });
                    video.srcObject = cameraStream;
                    await video.play();
                    lastDetectedBarcode = "";
                    startCameraButton.disabled = true;
                    stopCameraButton.disabled = false;
                    setText("mobileScannerStatus", "Kamera aktiv.");
                    scanFrame();
                } catch (error) {
                    stopCamera(false);
                    setText("mobileScannerStatus", "Kamera konnte nicht gestartet werden.");
                }
            }

            startCameraButton?.addEventListener("click", startCamera);
            stopCameraButton?.addEventListener("click", () => stopCamera());
            document.addEventListener("visibilitychange", () => {
                if (document.hidden) {
                    stopCamera();
                }
            });
        })();
"""


def mobile_next(panel: str) -> str:
    panel = "".join(char for char in panel if char.isalnum() or char in "-_") or "overview"
    return "/mobile?" + urlencode({"panel": panel})


def render_mobile_material_cards(materials: list[sqlite3.Row]) -> str:
    if not materials:
        return '<article class="mobile-card empty-card">Noch kein Material erfasst.</article>'

    cards = []
    for row in materials:
        available = row["quantity_available"]
        total = row["quantity_total"]
        loaned = row["quantity_loaned"]
        stock_class = "empty-stock" if available <= 0 else "ok-stock"
        barcode = row["barcode"] or "Kein Barcode"
        search_text = " ".join(
            str(value or "")
            for value in (
                row["barcode"],
                row["name"],
                row["category"],
                row["owner"],
                row["destination"],
            )
        ).casefold()
        cards.append(
            f"""
            <article class="mobile-card material-card" data-mobile-material-card data-search="{h(search_text)}">
                <div class="card-top">
                    <div>
                        <strong>{h(row['name'])}</strong>
                        <span class="subline">{h(row['category'])} | {h(row['owner'])}</span>
                    </div>
                    <span class="pill {stock_class}">{available} frei</span>
                </div>
                <div class="card-row">
                    <span class="subline">Gesamt {total}</span>
                    <span class="subline">Verliehen {loaned}</span>
                    <span class="subline">Ziel: {h(row['destination'])}</span>
                </div>
                <div class="card-row">
                    <span class="barcode-text">{h(barcode)}</span>
                </div>
                <div class="card-actions">
                    <button type="button" class="mini-button secondary-button" data-mobile-edit="{row['id']}">Bearbeiten</button>
                    <a class="mini-link" href="/labels/material/{row['id']}">Label</a>
                </div>
            </article>
            """
        )
    return "\n".join(cards)


def render_mobile_loan_cards(active_loans: list[sqlite3.Row]) -> str:
    if not active_loans:
        return '<article class="mobile-card empty-card">Aktuell ist nichts verliehen.</article>'

    rows = []
    for row in active_loans:
        note = f'<span class="subline">{h(row["note"])}</span>' if row["note"] else ""
        rows.append(
            f"""
            <article class="mobile-card">
                <div class="card-top">
                    <div>
                        <strong>{h(row['material_name'])}</strong>
                        <span class="subline">{h(row['material_category'])}</span>
                    </div>
                    <span class="pill">{row['quantity']} Stk.</span>
                </div>
                <div class="card-row">
                    <span class="subline">{h(row['user_name'])}</span>
                    <span class="subline">{h(row['location_name'])}</span>
                </div>
                <span class="subline">Seit {h(row['loaned_at'])}</span>
                {note}
                <form method="post" action="/loans/return" class="inline-form">
                    <input type="hidden" name="loan_id" value="{row['id']}">
                    <input type="hidden" name="next" value="{h(mobile_next('returns'))}">
                    <button type="submit">Rueckgabe buchen</button>
                </form>
            </article>
            """
        )
    return "\n".join(rows)


def render_mobile_movement_cards(movements: list[sqlite3.Row]) -> str:
    if not movements:
        return '<article class="mobile-card empty-card">Noch keine Buchungen vorhanden.</article>'

    labels = {
        "in": "Einbuchung",
        "out": "Ausbuchung",
        "loan": "Verleih",
        "return": "Rueckgabe",
        "issue": "Ausgabe",
    }
    rows = []
    for row in movements:
        rows.append(
            f"""
            <article class="mobile-card">
                <div class="card-top">
                    <div>
                        <strong>{h(labels.get(row['action'], row['action']))}</strong>
                        <span class="subline">{h(row['material_name'])}</span>
                    </div>
                    <span class="pill">{row['quantity']} Stk.</span>
                </div>
                <span class="subline">{h(row['created_at'])}</span>
                <span class="subline">{h(row['note'])}</span>
            </article>
            """
        )
    return "\n".join(rows)


def render_mobile_user_cards(users: list[sqlite3.Row]) -> str:
    if not users:
        return '<article class="mobile-card empty-card">Noch keine Nutzer angelegt.</article>'

    return "\n".join(
        f"""
        <article class="mobile-card">
            <strong>{h(row['name'])}</strong>
            <span class="barcode-text">{h(row['barcode'])}</span>
            <span class="subline">{h(row['contact'])}</span>
            <span class="subline">{h(row['notes'])}</span>
            <div class="card-actions">
                <a class="mini-link" href="/labels/user/{row['id']}">Label</a>
            </div>
        </article>
        """
        for row in users
    )


def render_mobile_location_cards(locations: list[sqlite3.Row]) -> str:
    if not locations:
        return '<article class="mobile-card empty-card">Noch keine Orte oder Baustellen angelegt.</article>'

    return "\n".join(
        f"""
        <article class="mobile-card">
            <strong>{h(row['name'])}</strong>
            <span class="barcode-text">{h(row['barcode'])}</span>
            <span class="subline">Angelegt {h(row['created_at'])}</span>
            <div class="card-actions">
                <a class="mini-link" href="/labels/location/{row['id']}">Label</a>
            </div>
        </article>
        """
        for row in locations
    )


def render_mobile_page(query: dict[str, list[str]]) -> str:
    data = get_view_data()
    materials = data["materials"]
    users = data["users"]
    locations = data["locations"]
    active_loans = data["active_loans"]
    recent_movements = data["recent_movements"]
    stats = data["stats"]

    message = query.get("message", [""])[0]
    kind = query.get("kind", ["ok"])[0]
    flash = ""
    if message:
        flash = f'<div class="flash {h(kind)}">{h(message)}</div>'

    material_select_disabled = "" if materials else "disabled"
    user_select_disabled = "" if users else "disabled"
    location_select_disabled = "" if locations else "disabled"
    disabled_for_loan = "" if materials and users and locations else "disabled"
    disabled_for_scan = "" if materials else "disabled"
    loan_hint = ""
    missing_loan_parts = []
    if not materials:
        missing_loan_parts.append("Material")
    if not users:
        missing_loan_parts.append("Nutzer")
    if not locations:
        missing_loan_parts.append("Ort bzw. Baustelle")
    if missing_loan_parts:
        loan_hint = (
            '<article class="mobile-card empty-card">Zum Verleihen oder Ausgeben fehlt noch: '
            f'{h(", ".join(missing_loan_parts))}.</article>'
        )

    client_data = json_for_script(
        {
            "materials": [serialize_material(row) for row in materials],
            "users": [serialize_user(row) for row in users],
            "locations": [serialize_location(row) for row in locations],
        }
    )

    overview_next = h(mobile_next("overview"))
    scanner_next = h(mobile_next("scanner"))
    stock_next = h(mobile_next("stock"))
    loan_next = h(mobile_next("loan"))
    people_next = h(mobile_next("people"))

    return f"""<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#1f2d2a">
    <title>Mobile - {h(APP_TITLE)}</title>
    <style>
{MOBILE_STYLE}
    </style>
</head>
<body>
    <header class="mobile-app-header">
        <div>
            <span>Mobile</span>
            <h1>Warenwirtschaft</h1>
        </div>
        <a class="header-link" href="/">Desktop</a>
    </header>

    <main class="mobile-main">
        {flash}

        <section class="mobile-stats" aria-label="Kennzahlen">
            <div class="mobile-stat"><span>Material</span><strong>{stats['material_count']}</strong></div>
            <div class="mobile-stat"><span>Frei</span><strong>{stats['available_sum']}</strong></div>
            <div class="mobile-stat"><span>Verliehen</span><strong>{stats['loaned_sum']}</strong></div>
            <div class="mobile-stat"><span>Leihen</span><strong>{stats['active_loan_count']}</strong></div>
        </section>

        <section id="mobile-overview" class="mobile-panel active">
            <div class="mobile-section">
                <h2>Bestand</h2>
                <input id="mobileSearch" class="search-input" type="search" placeholder="Suchen" autocomplete="off">
                <div class="mobile-list">
                    {render_mobile_material_cards(materials)}
                </div>
            </div>
            <div class="mobile-section">
                <h2>Letzte Buchungen</h2>
                <div class="mobile-list">
                    {render_mobile_movement_cards(recent_movements)}
                </div>
            </div>
        </section>

        <section id="mobile-scanner" class="mobile-panel">
            <div class="mobile-section">
                <h2>Scanner</h2>
                <video id="mobileBarcodeVideo" class="scanner-video" playsinline muted></video>
                <div id="mobileScannerStatus" class="scanner-status">Kamera aus.</div>
                <div class="button-row">
                    <button type="button" id="mobileStartCamera" {disabled_for_scan}>Kamera starten</button>
                    <button type="button" id="mobileStopCamera" class="secondary-button" disabled>Stopp</button>
                </div>
                <form id="mobileScanForm" class="mobile-form">
                    <label>Barcode
                        <input id="mobileScanBarcode" class="scan-input" placeholder="Barcode scannen oder eintippen" autocomplete="off" {disabled_for_scan}>
                    </label>
                    <button type="submit" {disabled_for_scan}>Material oeffnen</button>
                </form>
                <form method="post" action="/scan/batch" class="mobile-form" id="mobileBatchScanForm">
                    <input type="hidden" name="next" value="{scanner_next}">
                    <input type="hidden" name="codes" id="mobileBatchScanCodes">
                    <h3>Stapel-Scan</h3>
                    <label>Buchungsart
                        <select name="action" id="mobileBatchScanAction" {disabled_for_scan}>
                            <option value="loan">Verleih / Ausgabe</option>
                            <option value="in">Einbuchen</option>
                            <option value="out">Ausbuchen</option>
                        </select>
                    </label>
                    <label>Code
                        <input id="mobileBatchScanInput" class="scan-input" placeholder="Material-, Nutzer- oder Ort-Code" autocomplete="off" {disabled_for_scan}>
                    </label>
                    <label>Ort / Baustelle
                        <select name="location_id" id="mobileBatchScanLocation" {location_select_disabled}>{render_location_options(locations)}</select>
                    </label>
                    <label>Notiz
                        <input name="note" placeholder="optional" {disabled_for_scan}>
                    </label>
                    <div id="mobileBatchScanStatus" class="scanner-status">Bereit.</div>
                    <div id="mobileBatchScanList" class="scan-list"></div>
                    <div class="button-row">
                        <button type="button" id="mobileBatchScanClear" class="secondary-button" {disabled_for_scan}>Liste leeren</button>
                        <button type="submit" id="mobileBatchScanSubmit" {disabled_for_scan}>Stapel buchen</button>
                    </div>
                </form>
            </div>
        </section>

        <section id="mobile-stock" class="mobile-panel">
            <div class="mobile-section">
                <h2>Einbuchen</h2>
                <form method="post" action="/materials/add" class="mobile-form">
                    <input type="hidden" name="next" value="{stock_next}">
                    <label>Art des Materials
                        <input name="name" placeholder="z.B. Akkuschrauber Bosch GSR 18V" required>
                    </label>
                    <label>Kategorie
                        <input name="category" list="mobileCategories" placeholder="z.B. Werkzeug" required>
                    </label>
                    <label>Besitzer
                        <input name="owner" placeholder="z.B. Firma, Peter, Lager" required>
                    </label>
                    <label>Bestimmungsort
                        <input name="destination" placeholder="z.B. Lager A, Baustelle Mitte" required>
                    </label>
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required>
                    </label>
                    <button type="submit">Einbuchen</button>
                    <datalist id="mobileCategories">
                        <option value="Werkzeug"></option>
                        <option value="Verbrauchsmaterial"></option>
                        <option value="Maschine"></option>
                        <option value="Baumaterial"></option>
                        <option value="Schutzkleidung"></option>
                    </datalist>
                </form>
            </div>

            <div class="mobile-section">
                <h2>Ausbuchen</h2>
                <form method="post" action="/materials/remove" class="mobile-form">
                    <input type="hidden" name="next" value="{stock_next}">
                    {render_material_picker(materials, only_available=True, disabled=material_select_disabled)}
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required {material_select_disabled}>
                    </label>
                    <label>Grund / Notiz
                        <input name="note" placeholder="z.B. verbraucht, defekt, verkauft" {material_select_disabled}>
                    </label>
                    <button type="submit" {material_select_disabled}>Ausbuchen</button>
                </form>
            </div>
        </section>

        <section id="mobile-loan" class="mobile-panel">
            <div class="mobile-section">
                <h2>Verleih / Ausgabe</h2>
                {loan_hint}
                <form method="post" action="/loans/add" class="mobile-form">
                    <input type="hidden" name="next" value="{loan_next}">
                    {render_material_picker(materials, only_available=True, disabled=material_select_disabled, material_select_id="mobileLoanMaterial")}
                    <label>Nutzer-Code
                        <input id="mobileLoanUserBarcode" class="scan-input" placeholder="Nutzer-Code scannen" autocomplete="off" {user_select_disabled}>
                    </label>
                    <label>Nutzer
                        <select name="user_id" id="mobileLoanUser" required {user_select_disabled}>{render_user_options(users)}</select>
                    </label>
                    <label>Ort-Code
                        <input id="mobileLoanLocationBarcode" class="scan-input" placeholder="Ort-Code scannen" autocomplete="off" {location_select_disabled}>
                    </label>
                    <label>Ort / Baustelle
                        <select name="location_id" id="mobileLoanLocation" required {location_select_disabled}>{render_location_options(locations)}</select>
                    </label>
                    <label>Anzahl
                        <input name="quantity" type="number" min="1" step="1" value="1" required {disabled_for_loan}>
                    </label>
                    <label>Notiz
                        <input name="note" placeholder="optional" {disabled_for_loan}>
                    </label>
                    <button type="submit" {disabled_for_loan}>Buchen</button>
                </form>
            </div>
        </section>

        <section id="mobile-returns" class="mobile-panel">
            <div class="mobile-section">
                <h2>Rueckgaben</h2>
                <div class="mobile-list">
                    {render_mobile_loan_cards(active_loans)}
                </div>
            </div>
        </section>

        <section id="mobile-people" class="mobile-panel">
            <div class="mobile-section">
                <h2>Nutzer</h2>
                <div class="button-row">
                    <a class="mini-link" href="/labels/users">Nutzer-Labels</a>
                </div>
                <form method="post" action="/users/add" class="mobile-form">
                    <input type="hidden" name="next" value="{people_next}">
                    <label>Name
                        <input name="name" placeholder="z.B. Anna Schneider" required>
                    </label>
                    <label>Kontakt
                        <input name="contact" placeholder="Telefon oder E-Mail, optional">
                    </label>
                    <label>Notiz
                        <textarea name="notes" placeholder="optional"></textarea>
                    </label>
                    <button type="submit">Nutzer erstellen</button>
                </form>
                <div class="mobile-list">
                    {render_mobile_user_cards(users)}
                </div>
            </div>

            <div class="mobile-section">
                <h2>Orte / Baustellen</h2>
                <div class="button-row">
                    <a class="mini-link" href="/labels/locations">Ort-Labels</a>
                </div>
                <form method="post" action="/locations/add" class="mobile-form">
                    <input type="hidden" name="next" value="{people_next}">
                    <label>Name
                        <input name="name" placeholder="z.B. Baustelle Hauptstrasse" required>
                    </label>
                    <button type="submit">Ort speichern</button>
                </form>
                <div class="mobile-list">
                    {render_mobile_location_cards(locations)}
                </div>
            </div>
        </section>
    </main>

    <nav class="mobile-bottom-tabs" aria-label="Mobile Bereiche">
        <button class="mobile-tab-button active" type="button" data-mobile-panel-button="mobile-overview">Bestand</button>
        <button class="mobile-tab-button" type="button" data-mobile-panel-button="mobile-scanner">Scan</button>
        <button class="mobile-tab-button" type="button" data-mobile-panel-button="mobile-stock">Buchen</button>
        <button class="mobile-tab-button" type="button" data-mobile-panel-button="mobile-loan">Verleih</button>
        <button class="mobile-tab-button" type="button" data-mobile-panel-button="mobile-returns">Rueckgabe</button>
        <button class="mobile-tab-button" type="button" data-mobile-panel-button="mobile-people">Daten</button>
    </nav>

    <dialog id="mobileMaterialEditDialog" class="mobile-dialog">
        <div class="dialog-shell">
            <div class="dialog-header">
                <div>
                    <h2>Artikel bearbeiten</h2>
                    <span class="subline" id="mobileMaterialEditMeta"></span>
                </div>
                <button type="button" class="icon-button" aria-label="Schliessen" data-close-dialog>&times;</button>
            </div>
            <form method="post" action="/materials/update" class="mobile-form" id="mobileMaterialEditForm">
                <input type="hidden" name="material_id">
                <input type="hidden" name="next" value="{overview_next}">
                <label>Art des Materials
                    <input name="name" required>
                </label>
                <label>Kategorie
                    <input name="category" list="mobileCategories" required>
                </label>
                <label>Besitzer
                    <input name="owner" required>
                </label>
                <label>Bestimmungsort
                    <input name="destination" required>
                </label>
                <button type="submit">Speichern</button>
            </form>
        </div>
    </dialog>

    <dialog id="mobileScanDialog" class="mobile-dialog">
        <div class="dialog-shell">
            <div class="dialog-header">
                <div>
                    <h2>Aktion waehlen</h2>
                    <span class="subline" id="mobileScanDialogBarcode"></span>
                </div>
                <button type="button" class="icon-button" aria-label="Schliessen" data-close-dialog>&times;</button>
            </div>
            <div class="material-summary" id="mobileScanSummary">
                <strong>Kein Material gewaehlt</strong>
            </div>
            <div class="action-choice-grid">
                <button type="button" class="action-choice-button" id="mobileScanActionLoan">
                    <strong>Verleih</strong>
                    <span>Nutzer und Ort auswaehlen.</span>
                </button>
                <button type="button" class="action-choice-button" id="mobileScanActionIn">
                    <strong>Einbuchen</strong>
                    <span>Bestand fuer diesen Barcode erhoehen.</span>
                </button>
                <button type="button" class="action-choice-button" id="mobileScanActionOut">
                    <strong>Ausbuchen</strong>
                    <span>Frei verfuegbaren Bestand entfernen.</span>
                </button>
            </div>
        </div>
    </dialog>

    <dialog id="mobileScanBookingDialog" class="mobile-dialog">
        <div class="dialog-shell">
            <div class="dialog-header">
                <div>
                    <h2 id="mobileScanBookingTitle">Material buchen</h2>
                    <span class="subline">Per Barcode</span>
                </div>
                <button type="button" class="icon-button" aria-label="Schliessen" data-close-dialog>&times;</button>
            </div>
            <div class="material-summary" id="mobileScanBookingSummary">
                <strong>Kein Material gewaehlt</strong>
            </div>
            <form method="post" action="/scan/book" class="mobile-form" id="mobileScanBookingForm">
                <input type="hidden" name="next" value="{scanner_next}">
                <input type="hidden" name="action" id="mobileScanBookingAction">
                <input type="hidden" name="barcode" id="mobileScanBookingBarcode">
                <label>Anzahl
                    <input name="quantity" id="mobileScanBookingQuantity" type="number" min="1" step="1" value="1" required>
                </label>
                <label>Notiz
                    <input name="note" id="mobileScanBookingNote" placeholder="optional">
                </label>
                <button type="submit" id="mobileScanBookingSubmit">Buchen</button>
            </form>
        </div>
    </dialog>

    <script>
        window.mobileAppData = {client_data};
    </script>
    <script>
{MOBILE_SCRIPT}
    </script>
</body>
</html>"""


def export_material_csv() -> bytes:
    data = get_view_data()
    materials = data["materials"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "barcode",
            "material",
            "kategorie",
            "besitzer",
            "bestimmungsort",
            "gesamt",
            "frei",
            "verliehen",
            "angelegt",
            "aktualisiert",
        ]
    )
    for row in materials:
        writer.writerow(
            [
                row["id"],
                row["barcode"],
                row["name"],
                row["category"],
                row["owner"],
                row["destination"],
                row["quantity_total"],
                row["quantity_available"],
                row["quantity_loaned"],
                row["created_at"],
                row["updated_at"],
            ]
        )
    return buffer.getvalue().encode("utf-8")


class InventoryHandler(BaseHTTPRequestHandler):
    routes = {
        "/materials/add": add_material,
        "/materials/remove": remove_material,
        "/materials/update": update_material,
        "/materials/destination": update_material_destination,
        "/scan/book": book_by_barcode,
        "/scan/batch": book_batch_by_codes,
        "/users/add": add_user,
        "/locations/add": add_location,
        "/loans/add": add_loan,
        "/loans/return": return_loan,
    }

    def do_GET(self) -> None:
        try:
            self.handle_get()
        except sqlite3.OperationalError as exc:
            self.log_error("Datenbank temporaer nicht erreichbar: %s", exc)
            self.send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Datenbank ist gerade ausgelastet. Bitte kurz erneut versuchen.",
            )
        except sqlite3.DatabaseError as exc:
            self.log_error("Datenbankfehler: %s", exc)
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Datenbankfehler. Bitte die Sicherungen pruefen.",
            )

    def handle_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            self.send_html(render_page(query))
            return

        if parsed.path == "/mobile":
            query = parse_qs(parsed.query)
            self.send_html(render_mobile_page(query))
            return

        if parsed.path == "/labels/materials":
            self.send_html(
                render_labels_page("Material-Labels", render_label_cards(get_label_materials()))
            )
            return

        if parsed.path.startswith("/labels/material/"):
            try:
                material_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Ungueltiges Material")
                return
            materials = get_label_materials(material_id)
            if not materials:
                self.send_error(HTTPStatus.NOT_FOUND, "Material nicht gefunden")
                return
            self.send_html(
                render_labels_page("Material-Label", render_label_cards(materials))
            )
            return

        if parsed.path == "/labels/users":
            self.send_html(
                render_labels_page("Nutzer-Labels", render_user_label_cards(get_label_users()))
            )
            return

        if parsed.path.startswith("/labels/user/"):
            try:
                user_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Ungueltiger Nutzer")
                return
            users = get_label_users(user_id)
            if not users:
                self.send_error(HTTPStatus.NOT_FOUND, "Nutzer nicht gefunden")
                return
            self.send_html(
                render_labels_page("Nutzer-Label", render_user_label_cards(users))
            )
            return

        if parsed.path == "/labels/locations":
            self.send_html(
                render_labels_page("Ort-Labels", render_location_label_cards(get_label_locations()))
            )
            return

        if parsed.path.startswith("/labels/location/"):
            try:
                location_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Ungueltiger Ort")
                return
            locations = get_label_locations(location_id)
            if not locations:
                self.send_error(HTTPStatus.NOT_FOUND, "Ort nicht gefunden")
                return
            self.send_html(
                render_labels_page("Ort-Label", render_location_label_cards(locations))
            )
            return

        if parsed.path == "/export/material.csv":
            body = export_material_csv()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="material_export.csv"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Nicht gefunden")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/users":
            self.create_api_user()
            return
        if parsed.path == "/api/locations":
            self.create_api_location()
            return

        handler = self.routes.get(parsed.path)
        if not handler:
            self.send_error(HTTPStatus.NOT_FOUND, "Nicht gefunden")
            return

        form: dict[str, str] = {}
        try:
            form = self.read_form()
            message = handler(form)
            self.redirect(message, "ok", form.get("next", ""))
        except AppError as exc:
            self.redirect(str(exc), "error", form.get("next", ""))
        except sqlite3.OperationalError as exc:
            self.log_error("Datenbank temporaer nicht erreichbar: %s", exc)
            self.redirect(
                "Datenbank ist gerade ausgelastet. Bitte kurz erneut versuchen.",
                "error",
                form.get("next", ""),
            )
        except sqlite3.DatabaseError as exc:
            self.log_error("Datenbankfehler: %s", exc)
            self.redirect(
                "Datenbankfehler. Bitte die Sicherungen pruefen.",
                "error",
                form.get("next", ""),
            )
        except Exception as exc:
            self.log_error("Unerwarteter Fehler: %s", exc)
            self.redirect(
                "Unerwarteter Fehler. Details stehen im Terminal.",
                "error",
                form.get("next", ""),
            )

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1].strip() for key, values in parsed.items()}

    def create_api_user(self) -> None:
        try:
            user = create_user_from_form(self.read_form())
            self.send_json({"ok": True, "user": serialize_user(user)})
        except AppError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.OperationalError as exc:
            self.log_error("Datenbank temporaer nicht erreichbar: %s", exc)
            self.send_json(
                {"ok": False, "error": "Datenbank ist gerade ausgelastet."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except sqlite3.DatabaseError as exc:
            self.log_error("Datenbankfehler: %s", exc)
            self.send_json(
                {"ok": False, "error": "Datenbankfehler. Bitte die Sicherungen pruefen."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:
            self.log_error("Unerwarteter API-Fehler: %s", exc)
            self.send_json(
                {"ok": False, "error": "Unerwarteter Fehler. Details stehen im Terminal."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def create_api_location(self) -> None:
        try:
            location = create_location_from_form(self.read_form())
            self.send_json({"ok": True, "location": serialize_location(location)})
        except AppError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.OperationalError as exc:
            self.log_error("Datenbank temporaer nicht erreichbar: %s", exc)
            self.send_json(
                {"ok": False, "error": "Datenbank ist gerade ausgelastet."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except sqlite3.DatabaseError as exc:
            self.log_error("Datenbankfehler: %s", exc)
            self.send_json(
                {"ok": False, "error": "Datenbankfehler. Bitte die Sicherungen pruefen."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:
            self.log_error("Unerwarteter API-Fehler: %s", exc)
            self.send_json(
                {"ok": False, "error": "Unerwarteter Fehler. Details stehen im Terminal."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html_body: str) -> None:
        body = html_body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, message: str, kind: str, return_to: str = "") -> None:
        location = safe_return_path(return_to)
        separator = "&" if "?" in location else "?"
        location = location + separator + urlencode({"message": message, "kind": kind})
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        timestamp = now_iso()
        print(f"[{timestamp}] {self.address_string()} - {fmt % args}")


class ReliableThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(result[4][0])
    except OSError:
        pass

    return sorted(address for address in addresses if not address.startswith("127."))


def local_network_hostnames() -> list[str]:
    hostnames: set[str] = set()

    def add_hostname(value: str) -> None:
        hostname = value.strip().rstrip(".")
        if (
            not hostname
            or hostname == "localhost"
            or hostname.endswith(".in-addr.arpa")
            or is_ip_address(hostname)
        ):
            return
        hostnames.add(hostname)
        if "." not in hostname:
            hostnames.add(f"{hostname}.local")

    for candidate in (socket.gethostname(), socket.getfqdn()):
        add_hostname(candidate)

    try:
        result = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        if result.returncode == 0:
            add_hostname(result.stdout)

    return sorted(hostnames)


def is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def certificate_hosts(bind_host: str) -> list[str]:
    hosts = {LOCAL_DOMAIN, "localhost", "127.0.0.1"}
    if bind_host not in {"0.0.0.0", ""}:
        hosts.add(bind_host)
    hosts.update(local_network_hostnames())
    hosts.update(local_ipv4_addresses())
    return sorted(hosts, key=lambda value: (0 if is_ip_address(value) else 1, value))


def write_openssl_config(path: Path, hosts: list[str]) -> None:
    dns_entries = []
    ip_entries = []
    for host in hosts:
        if is_ip_address(host):
            ip_entries.append(host)
        else:
            dns_entries.append(host)

    alt_lines = []
    for index, value in enumerate(dns_entries, start=1):
        alt_lines.append(f"DNS.{index} = {value}")
    for index, value in enumerate(ip_entries, start=1):
        alt_lines.append(f"IP.{index} = {value}")

    config = f"""
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = {APP_TITLE} Lokal

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
{chr(10).join(alt_lines)}
""".lstrip()
    path.write_text(config, encoding="utf-8")


def ensure_https_certificate(bind_host: str) -> tuple[Path, Path]:
    hosts = certificate_hosts(bind_host)
    host_signature = "\n".join(hosts)
    current_signature = TLS_HOSTS_PATH.read_text(encoding="utf-8") if TLS_HOSTS_PATH.exists() else ""
    if (
        TLS_CERT_PATH.exists()
        and TLS_KEY_PATH.exists()
        and current_signature == host_signature
    ):
        return TLS_CERT_PATH, TLS_KEY_PATH

    TLS_DIR.mkdir(exist_ok=True)
    write_openssl_config(TLS_CONFIG_PATH, hosts)
    command = [
        "openssl",
        "req",
        "-x509",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(TLS_KEY_PATH),
        "-out",
        str(TLS_CERT_PATH),
        "-days",
        "825",
        "-config",
        str(TLS_CONFIG_PATH),
        "-sha256",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "HTTPS-Zertifikat konnte nicht erzeugt werden. "
            "Bitte pruefen, ob openssl installiert ist."
        ) from exc

    TLS_KEY_PATH.chmod(0o600)
    TLS_HOSTS_PATH.write_text(host_signature, encoding="utf-8")
    return TLS_CERT_PATH, TLS_KEY_PATH


def enable_https(server: ThreadingHTTPServer, cert_path: Path, key_path: Path) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)


def display_urls(host: str, port: int, *, https: bool = False) -> list[str]:
    scheme = "https" if https else "http"
    if host in {"0.0.0.0", ""}:
        hosts = [LOCAL_DOMAIN, *local_network_hostnames(), "127.0.0.1", *local_ipv4_addresses()]
    else:
        hosts = [host]
    default_port = 443 if https else 80
    port_part = "" if port == default_port else f":{port}"
    return [f"{scheme}://{address}{port_part}" for address in dict.fromkeys(hosts)]


def make_server(host: str, start_port: int) -> tuple[ThreadingHTTPServer, int]:
    for port in range(start_port, start_port + 50):
        try:
            server = ReliableThreadingHTTPServer((host, port), InventoryHandler)
            return server, port
        except OSError:
            continue
    raise RuntimeError(f"Kein freier Port zwischen {start_port} und {start_port + 49}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host/IP zum Lauschen, Standard: 0.0.0.0 fuer das lokale Netzwerk",
    )
    parser.add_argument("--port", type=int, default=8000, help="Start-Port, Standard: 8000")
    parser.add_argument(
        "--https",
        action="store_true",
        help="HTTPS mit lokal erzeugtem Zertifikat aktivieren; noetig fuer Handy-Kamera.",
    )
    parser.add_argument(
        "--backup-interval-hours",
        type=float,
        default=24,
        help="Intervall fuer laufende Datenbanksicherungen, Standard: 24",
    )
    parser.add_argument(
        "--no-scheduled-backups",
        action="store_true",
        help="Laufende Sicherungen deaktivieren; Start-Sicherung bleibt aktiv.",
    )
    args = parser.parse_args()
    if args.backup_interval_hours <= 0:
        parser.error("--backup-interval-hours muss groesser als 0 sein.")

    init_db()
    server, port = make_server(args.host, args.port)
    cert_path = None
    if args.https:
        cert_path, key_path = ensure_https_certificate(args.host)
        enable_https(server, cert_path, key_path)
    backup_stop_event = threading.Event()
    backup_thread = None
    backup_interval = timedelta(hours=args.backup_interval_hours)
    if not args.no_scheduled_backups:
        backup_thread = threading.Thread(
            target=run_backup_scheduler,
            args=(backup_stop_event, backup_interval),
            name="datenbank-backups",
            daemon=True,
        )
        backup_thread.start()

    urls = display_urls(args.host, port, https=args.https)
    print(f"{APP_TITLE} laeuft auf {urls[0]}")
    print(f"Handy-Ansicht: {urls[-1]}/mobile")
    if len(urls) > 1:
        print("Im gleichen WLAN auch erreichbar unter:")
        for url in urls[1:]:
            print(f"  {url}")
    if args.https and cert_path:
        print(f"HTTPS-Zertifikat: {cert_path}")
        print("Falls der Browser warnt: Zertifikat/Verbindung fuer dieses lokale Geraet akzeptieren.")
    if args.host in {"0.0.0.0", ""}:
        print("Hinweis: Die App ist im lokalen Netzwerk erreichbar; nur in vertrauenswuerdigen Netzen starten.")
    if args.no_scheduled_backups:
        print("Regelmaessige Datenbanksicherungen sind deaktiviert.")
    else:
        print(f"Regelmaessige Datenbanksicherungen alle {args.backup_interval_hours:g} Stunden.")
    print("Mit Strg+C beenden.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        backup_stop_event.set()
        if backup_thread:
            backup_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()

"""SQLite access: one connection per request, WAL mode, explicit write transactions."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "medbridge.db"

# Parents before children: created in this order, dropped in reverse.
TABLES = {
    "patients": """
        CREATE TABLE IF NOT EXISTS patients (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )""",
    "drug_master": """
        CREATE TABLE IF NOT EXISTS drug_master (
            brand             TEXT,
            generic           TEXT,
            strength          TEXT,
            dosage_form       TEXT,
            manufacturer_name TEXT
        )""",
    "documents": """
        CREATE TABLE IF NOT EXISTS documents (
            id         INTEGER PRIMARY KEY,
            patient_id INTEGER NOT NULL REFERENCES patients(id),
            facility   TEXT,
            doc_type   TEXT,
            doc_date   TEXT,
            prescriber TEXT,
            sha256     TEXT NOT NULL,
            raw_text   TEXT NOT NULL,
            UNIQUE (patient_id, sha256)
        )""",
    "med_entries": """
        CREATE TABLE IF NOT EXISTS med_entries (
            id                INTEGER PRIMARY KEY,
            document_id       INTEGER NOT NULL REFERENCES documents(id),
            drug_raw          TEXT NOT NULL,
            drug_norm         TEXT NOT NULL,
            dose              NUMERIC,
            unit              TEXT,
            frequency_per_day INTEGER,
            route             TEXT,
            effective_date    TEXT,
            note              TEXT,
            source_snippet    TEXT NOT NULL,
            confidence        REAL NOT NULL
        )""",
    "discrepancies": """
        CREATE TABLE IF NOT EXISTS discrepancies (
            id             INTEGER PRIMARY KEY,
            patient_id     INTEGER NOT NULL REFERENCES patients(id),
            type           TEXT NOT NULL,
            drug_norm      TEXT NOT NULL,
            entry_a_id     INTEGER REFERENCES med_entries(id),
            entry_b_id     INTEGER REFERENCES med_entries(id),
            classification TEXT,
            fingerprint    TEXT NOT NULL UNIQUE,
            status         TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open', 'claimed', 'resolved')),
            version        INTEGER NOT NULL DEFAULT 1,
            claimed_by     TEXT,
            gate_decision  TEXT,
            missing_facts  TEXT
        )""",
    "facts": """
        CREATE TABLE IF NOT EXISTS facts (
            id             INTEGER PRIMARY KEY,
            discrepancy_id INTEGER NOT NULL REFERENCES discrepancies(id),
            name           TEXT NOT NULL,
            value          TEXT NOT NULL,
            source         TEXT NOT NULL,
            added_by       TEXT NOT NULL
        )""",
    "resolutions": """
        CREATE TABLE IF NOT EXISTS resolutions (
            id             INTEGER PRIMARY KEY,
            discrepancy_id INTEGER NOT NULL REFERENCES discrepancies(id),
            action         TEXT NOT NULL,
            actor          TEXT NOT NULL,
            note           TEXT,
            created_at     TEXT NOT NULL
        )""",
    "idempotency": """
        CREATE TABLE IF NOT EXISTS idempotency (
            "key"         TEXT NOT NULL,
            endpoint      TEXT NOT NULL,
            actor         TEXT NOT NULL,
            request_hash  TEXT NOT NULL,
            status_code   INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            PRIMARY KEY ("key", endpoint, actor)
        )""",
    "audit_log": """
        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY,
            ts        TEXT NOT NULL,
            actor     TEXT NOT NULL,
            action    TEXT NOT NULL,
            entity    TEXT NOT NULL,
            payload   TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash      TEXT NOT NULL
        )""",
}


def connect():
    # isolation_level=None stops Python opening its own implicit transactions,
    # so the explicit BEGIN IMMEDIATE on each write path is the only one.
    conn = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction():
    """Yield a connection inside BEGIN IMMEDIATE and commit when the block ends.

    If the block raises, COMMIT never runs and closing the connection rolls back.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    finally:
        conn.close()


def create_tables(conn):
    """Runs inside the caller's transaction. Not executescript, which would commit it."""
    for ddl in TABLES.values():
        conn.execute(ddl)


def drop_tables(conn):
    """Runs inside the caller's transaction. Children first, so foreign keys stay satisfied."""
    for name in reversed(TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def init_db():
    with transaction() as conn:
        create_tables(conn)

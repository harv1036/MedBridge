"""Rebuild the demo database. Run `python seed.py`, or call reset() from POST /admin/reset."""
import csv
from pathlib import Path

from app.db import connect, create_tables, drop_tables, transaction
from app.extract import ingest_document

ROOT = Path(__file__).resolve().parent
DRUG_MASTER_CSV = ROOT / "data" / "drug_master.csv"
DOCS_DIR = ROOT / "data" / "docs"
DRUG_COLUMNS = ("brand", "generic", "strength", "dosage_form", "manufacturer_name")

PATIENTS = {1: "Ravi", 2: "Meera", 3: "Arjun", 4: "Sunita"}

# The documents have no patient line, so their owner is recorded here.
DOC_OWNERS = {
    "doc1.txt": 1,
    "doc2.txt": 1,
    "doc3.txt": 2,
    "doc4.txt": 2,
    "doc5.txt": 3,
    "doc6.txt": 3,
    "doc7.txt": 4,
    "doc8.txt": 4,
}


def read_drug_master():
    if not DRUG_MASTER_CSV.exists():
        raise FileNotFoundError(f"{DRUG_MASTER_CSV} not found. Put drug_master.csv in data/ and run again.")
    with DRUG_MASTER_CSV.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
        missing = [column for column in DRUG_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise ValueError(f"{DRUG_MASTER_CSV.name} has no column(s) {missing}. Found: {reader.fieldnames}")
        return [[row[column] for column in DRUG_COLUMNS] for row in reader]


def reset():
    """Empty and rebuild every table in one transaction.

    The database file is kept, not deleted: Windows refuses to delete a SQLite file that
    any request still has open, which a polling page makes likely. One transaction also
    means other requests see the old data or the new data, never a half-built database.
    """
    drugs = read_drug_master()
    with transaction() as conn:
        drop_tables(conn)
        create_tables(conn)
        conn.executemany("INSERT INTO drug_master VALUES (?, ?, ?, ?, ?)", drugs)
        conn.executemany("INSERT INTO patients (id, name) VALUES (?, ?)", PATIENTS.items())
        for filename, patient_id in DOC_OWNERS.items():
            ingest_document(conn, patient_id, (DOCS_DIR / filename).read_bytes().decode("utf-8"))


if __name__ == "__main__":
    reset()
    conn = connect()
    for table in ("patients", "documents", "drug_master", "med_entries", "discrepancies"):
        print(f"{table}: {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    conn.close()

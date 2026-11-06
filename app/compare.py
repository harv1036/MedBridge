"""Compare one patient's documents with plain rules. Nothing here decides which record is right."""
import hashlib
import re

# Whole words, so a note saying "unchanged" does not count as "changed".
INTENTIONAL_WORDS = re.compile(r"\b(increased|decreased|changed|switched|stopped)\b", re.IGNORECASE)


def load_entries(conn, patient_id):
    """Oldest document first. Undated documents go last, in upload order."""
    return conn.execute(
        """
        SELECT e.*, d.facility, d.doc_type, d.doc_date, d.prescriber
        FROM med_entries e JOIN documents d ON d.id = e.document_id
        WHERE d.patient_id = ?
        ORDER BY d.doc_date IS NULL, d.doc_date, d.id, e.id
        """,
        (patient_id,),
    ).fetchall()


def group_by_drug(entries):
    groups = {}
    for entry in entries:
        groups.setdefault(entry["drug_norm"], []).append(entry)
    return groups


def find_conflicts(drug_entries):
    """Compare each document's entry with the next document's entry for the same drug."""
    for a, b in zip(drug_entries, drug_entries[1:]):
        if a["document_id"] == b["document_id"]:
            continue
        if (a["dose"], a["unit"]) != (b["dose"], b["unit"]):
            yield "DOSE_CONFLICT", a, b
        if a["frequency_per_day"] != b["frequency_per_day"]:
            yield "FREQUENCY_CONFLICT", a, b
        if a["route"] != b["route"]:
            yield "ROUTE_CONFLICT", a, b


def find_missing(drug_entries, documents):
    """Later documents that omit the drug. This records an absence only; it never means stopped."""
    position = {document["document_id"]: index for index, document in enumerate(documents)}
    mentioned_in = {entry["document_id"] for entry in drug_entries}
    for index, later in enumerate(documents):
        if later["document_id"] in mentioned_in:
            continue
        earlier = [
            entry
            for entry in drug_entries
            if position[entry["document_id"]] < index
            and (entry["facility"], entry["doc_type"]) != (later["facility"], later["doc_type"])
        ]
        if earlier:
            yield "MISSING", earlier[-1], None


def classify(later_entry):
    note = later_entry["note"] if later_entry else None
    return "intentional_change" if note and INTENTIONAL_WORDS.search(note) else "unexplained"


def compare_report(conn, patient_id):
    """Read-only view of the patient's entries, discrepancies and matched-by-generic notes."""
    groups = group_by_drug(load_entries(conn, patient_id))
    notes = []
    for drug, drug_entries in sorted(groups.items()):
        names = {entry["drug_raw"].lower(): entry["drug_raw"] for entry in drug_entries}
        if len(names) > 1:
            notes.append({"drug_norm": drug, "raw_names": sorted(names.values()), "note": "matched by generic"})
    discrepancies = conn.execute(
        "SELECT id, patient_id, type, drug_norm, entry_a_id, entry_b_id, classification, status, version,"
        " claimed_by, gate_decision, missing_facts FROM discrepancies WHERE patient_id = ? ORDER BY id",
        (patient_id,),
    ).fetchall()
    return {
        "patient_id": patient_id,
        "drugs": [
            {"drug_norm": drug, "entries": [dict(entry) for entry in drug_entries]}
            for drug, drug_entries in sorted(groups.items())
        ],
        "discrepancies": [dict(row) for row in discrepancies],
        "matched_by_generic": notes,
    }


def compare_patient(conn, patient_id):
    """Record any new discrepancies inside the caller's open transaction, then return the report."""
    entries = load_entries(conn, patient_id)
    # A document with no medicine lines says nothing about any medicine, so it never counts as an omission.
    documents = list({entry["document_id"]: entry for entry in entries}.values())
    for drug, drug_entries in group_by_drug(entries).items():
        for kind, a, b in (*find_conflicts(drug_entries), *find_missing(drug_entries, documents)):
            entry_b_id = b["id"] if b else None
            fingerprint = hashlib.sha256(
                f"{patient_id}|{kind}|{drug}|{a['id']}|{'' if entry_b_id is None else entry_b_id}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO discrepancies (patient_id, type, drug_norm, entry_a_id, entry_b_id,"
                " classification, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (patient_id, kind, drug, a["id"], entry_b_id, classify(b), fingerprint),
            )
    return compare_report(conn, patient_id)

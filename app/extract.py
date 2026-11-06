"""Parse the plain-text document format and store documents. A blank or unreadable field stays None."""
import hashlib
import re
from datetime import date

from app.compare import compare_patient
from app.normalise import normalise_name

HEADER_FIELDS = {"facility": "facility", "type": "doc_type", "date": "doc_date", "prescriber": "prescriber"}
FREQUENCY_PER_DAY = {"twice daily": 2, "bd": 2, "bid": 2, "once daily": 1, "od": 1, "thrice daily": 3, "tds": 3}
DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Zµ%/]+)?")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_date(text):
    if not ISO_DATE.fullmatch(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def parse_dose(text):
    match = DOSE.fullmatch(text)
    if not match:
        return None, None
    number = float(match.group(1))
    return (int(number) if number.is_integer() else number), (match.group(2).lower() if match.group(2) else None)


def parse_entry(line):
    fields = [field.strip() for field in line.split(":", 1)[1].split("|", 5)]
    name, dose_text, frequency_text, route, effective_text, note = fields + [""] * (6 - len(fields))
    dose, unit = parse_dose(dose_text)
    frequency = FREQUENCY_PER_DAY.get(" ".join(frequency_text.lower().split()))
    effective_date = parse_date(effective_text)
    # A blank effective date or note is a valid reading of the line, so it does not lower confidence;
    # the gate treats a null effective date as a missing fact. Text that cannot be read does lower it.
    parsed = name and dose is not None and unit and frequency and route and (effective_date or not effective_text)
    return {
        "drug_raw": name,
        "dose": dose,
        "unit": unit,
        "frequency_per_day": frequency,
        "route": route.lower() or None,
        "effective_date": effective_date,
        "note": note or None,
        "source_snippet": line,
        "confidence": 1.0 if parsed else 0.6,
    }


def parse_document(text):
    """Return (header, entries). Other lines are skipped here; raw_text still keeps them."""
    header = dict.fromkeys(HEADER_FIELDS.values())
    entries = []
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        label = label.strip(" \t﻿").lower()
        if not separator:
            continue
        if label == "med":
            entries.append(parse_entry(line))
        elif label in HEADER_FIELDS and header[HEADER_FIELDS[label]] is None:
            header[HEADER_FIELDS[label]] = value.strip() or None
    header["doc_date"] = parse_date(header["doc_date"] or "")
    return header, entries


def ingest_document(conn, patient_id, raw_text):
    """Store one document and compare the patient, inside the caller's open transaction."""
    sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM documents WHERE patient_id = ? AND sha256 = ?", (patient_id, sha256)
    ).fetchone()
    if existing:
        return {"document_id": existing["id"], "duplicate": True}

    header, entries = parse_document(raw_text)
    document_id = conn.execute(
        "INSERT INTO documents (patient_id, facility, doc_type, doc_date, prescriber, sha256, raw_text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (patient_id, header["facility"], header["doc_type"], header["doc_date"], header["prescriber"], sha256, raw_text),
    ).lastrowid
    for entry in entries:
        name = normalise_name(conn, entry["drug_raw"])
        conn.execute(
            "INSERT INTO med_entries (document_id, drug_raw, drug_norm, dose, unit, frequency_per_day, route,"
            " effective_date, note, source_snippet, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                entry["drug_raw"],
                name["drug_norm"],
                entry["dose"],
                entry["unit"],
                entry["frequency_per_day"],
                entry["route"],
                entry["effective_date"],
                entry["note"],
                entry["source_snippet"],
                min(entry["confidence"], name["confidence_cap"]),
            ),
        )
    compare_patient(conn, patient_id)
    return {"document_id": document_id, "duplicate": False}

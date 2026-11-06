"""Step 2: extraction, name matching and comparison rules."""
from contextlib import closing

import pytest

from app import db
from app.compare import compare_patient
from app.extract import ingest_document, parse_document
from app.normalise import name_key, normalise_name

TEST_PATIENT = 99


def doc(*meds, facility="Clinic A", doc_type="clinic", date="2026-01-01"):
    lines = [f"Facility: {facility}", f"Type: {doc_type}", f"Date: {date}", "Prescriber: Dr Test"]
    return "\n".join(lines + [f"MED: {med}" for med in meds]) + "\n"


def med(name="Metformin", dose="500 mg", frequency="once daily", route="oral", effective="2026-01-01", note=""):
    return f"{name} | {dose} | {frequency} | {route} | {effective} | {note}"


@pytest.fixture
def test_patient(seeded):
    """A patient with no documents, for checking one rule at a time."""
    with db.transaction() as conn:
        conn.execute("INSERT INTO patients (id, name) VALUES (?, 'Test')", (TEST_PATIENT,))
    return TEST_PATIENT


def ingest(patient_id, *texts):
    with db.transaction() as conn:
        return [ingest_document(conn, patient_id, text) for text in texts]


def query(sql, *params):
    with closing(db.connect()) as conn:
        return conn.execute(sql, params).fetchall()


def count(table):
    return query(f"SELECT COUNT(*) FROM {table}")[0][0]


def kinds(patient_id):
    rows = query("SELECT type, drug_norm, classification FROM discrepancies WHERE patient_id = ? ORDER BY id", patient_id)
    return [tuple(row) for row in rows]


def report(client, patient_id):
    response = client.get(f"/patients/{patient_id}/compare")
    assert response.status_code == 200
    return response.json()


def entries_by_id(data):
    return {entry["id"]: entry for drug in data["drugs"] for entry in drug["entries"]}


# ----- The seeded patients: the "done when" list for step 2 -----


def test_seed_counts(seeded):
    assert count("patients") == 4
    assert count("documents") == 8
    assert count("drug_master") > 600
    assert count("med_entries") == 10
    assert count("discrepancies") == 4


def test_patient_1_metformin_dose_conflict_is_unexplained(client):
    data = report(client, 1)
    assert kinds(1) == [("DOSE_CONFLICT", "metformin", "unexplained")]
    assert data["matched_by_generic"] == []
    conflict = data["discrepancies"][0]
    entries = entries_by_id(data)
    assert entries[conflict["entry_a_id"]]["dose"] == 500
    assert entries[conflict["entry_b_id"]]["dose"] == 1000
    assert entries[conflict["entry_b_id"]]["effective_date"] is None
    assert entries[conflict["entry_b_id"]]["source_snippet"] == "MED: Metformin | 1000 mg | twice daily | oral |  |"


def test_patient_2_amlodipine_is_missing_and_never_called_stopped(client):
    response = client.get("/patients/2/compare")
    data = response.json()
    assert kinds(2) == [("MISSING", "amlodipine", "unexplained")]
    assert data["discrepancies"][0]["entry_b_id"] is None
    assert entries_by_id(data)[data["discrepancies"][0]["entry_a_id"]]["drug_raw"] == "Amlodipine"
    assert "stopped" not in response.text.lower()
    assert "discontinued" not in response.text.lower()


def test_patient_3_glycomet_matches_metformin_and_atorvastatin_change_is_intentional(client):
    data = report(client, 3)
    assert kinds(3) == [("DOSE_CONFLICT", "atorvastatin", "intentional_change")]
    assert data["matched_by_generic"] == [
        {"drug_norm": "metformin", "raw_names": ["Glycomet", "Metformin"], "note": "matched by generic"}
    ]


def test_patient_4_telmisartan_change_is_intentional(client):
    data = report(client, 4)
    assert kinds(4) == [("DOSE_CONFLICT", "telmisartan", "intentional_change")]
    assert data["matched_by_generic"] == []


def test_comparing_twice_leaves_the_discrepancy_count_unchanged(client):
    before = count("discrepancies")
    for patient_id in (1, 2, 3, 4):
        with db.transaction() as conn:
            compare_patient(conn, patient_id)
            compare_patient(conn, patient_id)
        report(client, patient_id)
        report(client, patient_id)
    assert count("discrepancies") == before == 4


def test_report_groups_entries_by_drug_with_each_source_snippet(client):
    data = report(client, 3)
    assert [drug["drug_norm"] for drug in data["drugs"]] == ["atorvastatin", "metformin"]
    metformin = data["drugs"][1]["entries"]
    assert [entry["source_snippet"] for entry in metformin] == [
        "MED: Glycomet | 500 mg | twice daily | oral | 2026-01-15 |",
        "MED: Metformin | 500 mg | twice daily | oral | 2026-06-01 |",
    ]
    assert [entry["facility"] for entry in metformin] == ["Sunrise Clinic", "City Hospital"]


# ----- Extraction -----


@pytest.mark.parametrize(
    "text, expected",
    [
        ("twice daily", 2), ("BD", 2), ("BID", 2), ("bd", 2), ("Twice  Daily", 2),
        ("once daily", 1), ("OD", 1),
        ("thrice daily", 3), ("TDS", 3),
        ("as needed", None), ("TID", None), ("", None),
    ],
)
def test_frequency_words(text, expected):
    _, [entry] = parse_document(f"MED: {med(frequency=text)}")
    assert entry["frequency_per_day"] == expected
    assert entry["confidence"] == (1.0 if expected else 0.6)


def test_header_and_every_field_are_parsed():
    header, [entry] = parse_document(doc(med(dose="2.5 mg", route="Oral", note="increased after review")))
    assert header == {"facility": "Clinic A", "doc_type": "clinic", "doc_date": "2026-01-01", "prescriber": "Dr Test"}
    assert entry["drug_raw"] == "Metformin"
    assert (entry["dose"], entry["unit"]) == (2.5, "mg")
    assert entry["route"] == "oral"
    assert entry["effective_date"] == "2026-01-01"
    assert entry["note"] == "increased after review"
    assert entry["confidence"] == 1.0


def test_blank_effective_date_and_note_stay_none_and_keep_full_confidence():
    _, [entry] = parse_document("MED: Metformin | 1000 mg | twice daily | oral |  |")
    assert entry["effective_date"] is None
    assert entry["note"] is None
    assert entry["confidence"] == 1.0


@pytest.mark.parametrize(
    "line, field, value",
    [
        (med(dose="one tablet"), "dose", None),
        (med(dose="one tablet"), "unit", None),
        (med(dose="500"), "unit", None),
        (med(dose=""), "dose", None),
        (med(route=""), "route", None),
        (med(effective="next Monday"), "effective_date", None),
        (med(effective="2026-02-30"), "effective_date", None),
        ("Metformin", "dose", None),
        (" | 500 mg | once daily | oral | 2026-01-01 |", "drug_raw", ""),
    ],
)
def test_unreadable_fields_are_never_guessed_and_lower_confidence(line, field, value):
    _, [entry] = parse_document(f"MED: {line}")
    assert entry[field] == value
    assert entry["confidence"] == 0.6


def test_source_snippet_is_the_exact_original_line():
    line = "  MED:  Metformin |500mg|  BD | Oral |2026-01-01|  a note | with a pipe  "
    _, [entry] = parse_document(f"Facility: X\r\n{line}\r\nOther text\r\n")
    assert entry["source_snippet"] == line
    assert (entry["dose"], entry["unit"], entry["frequency_per_day"]) == (500, "mg", 2)
    assert entry["note"] == "a note | with a pipe"


def test_document_without_a_valid_date_has_no_doc_date():
    header, _ = parse_document("Facility: X\nDate: 03/04/2026\nMED: Metformin")
    assert header["doc_date"] is None


# ----- Name matching -----


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Glycomet", "metformin"),
        ("GLYCOMET", "metformin"),
        ("Glycomet Tablet", "metformin"),
        ("Tab. Glycomet 500 mg", "metformin"),
        ("Glycomet 500mg tab", "metformin"),
        ("Metformin", "metformin"),
        ("metformin 500mg tablets", "metformin"),
        ("Metoprolol Succinate", "metoprolol succinate"),
    ],
)
def test_brands_and_generics_match(seeded, raw, expected):
    with closing(db.connect()) as conn:
        assert normalise_name(conn, raw) == {"drug_norm": expected, "unmatched": False, "confidence_cap": 1.0}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Foo Bar 20", "foo bar 20"),
        ("Glycomet-GP 1", "glycomet-gp 1"),  # a combination product is not plain Glycomet
        ("500 mg", "500 mg"),
        ("Tablet", "tablet"),
        ("", ""),
    ],
)
def test_no_match_returns_the_lowercased_raw_name_with_capped_confidence(seeded, raw, expected):
    with closing(db.connect()) as conn:
        assert normalise_name(conn, raw) == {"drug_norm": expected, "unmatched": True, "confidence_cap": 0.7}


def test_a_name_listed_under_two_generics_is_not_guessed(seeded):
    with db.transaction() as conn:
        conn.execute("INSERT INTO drug_master (brand, generic) VALUES ('Twinbrand Tablet', 'Metformin')")
        conn.execute("INSERT INTO drug_master (brand, generic) VALUES ('Twinbrand 10 Tablet', 'Aspirin')")
    with closing(db.connect()) as conn:
        assert normalise_name(conn, "Twinbrand")["unmatched"] is True


def test_no_name_in_the_real_table_is_blank_or_maps_to_two_generics(seeded):
    generics_by_key = {}
    for row in query("SELECT brand, generic FROM drug_master"):
        generics_by_key.setdefault(name_key(row["brand"]), set()).add(row["generic"])
    assert "" not in generics_by_key
    assert [key for key, generics in generics_by_key.items() if len(generics) > 1] == []


def test_every_generic_in_the_real_table_maps_to_itself(seeded):
    generics = [row[0] for row in query("SELECT DISTINCT generic FROM drug_master")]
    assert len(generics) == 12
    with closing(db.connect()) as conn:
        for generic in generics:
            assert normalise_name(conn, generic)["drug_norm"] == generic.lower()


def test_unmatched_name_caps_the_stored_confidence(test_patient):
    ingest(test_patient, doc(med(name="Foo Bar")))
    rows = query("SELECT drug_norm, confidence FROM med_entries e JOIN documents d ON d.id = e.document_id"
                 " WHERE d.patient_id = ?", test_patient)
    assert [tuple(row) for row in rows] == [("foo bar", 0.7)]


# ----- Comparison rules -----


@pytest.mark.parametrize(
    "later, expected",
    [
        (med(effective="2026-02-01"), []),
        (med(dose="1000 mg"), ["DOSE_CONFLICT"]),
        (med(dose="500 mcg"), ["DOSE_CONFLICT"]),
        (med(frequency="twice daily"), ["FREQUENCY_CONFLICT"]),
        (med(route="iv"), ["ROUTE_CONFLICT"]),
        (med(route="ORAL"), []),
        (med(dose="1000 mg", frequency="twice daily"), ["DOSE_CONFLICT", "FREQUENCY_CONFLICT"]),
        (med(dose="one tablet"), ["DOSE_CONFLICT"]),  # an unreadable later dose is flagged, not ignored
        (med(frequency="as needed"), ["FREQUENCY_CONFLICT"]),
    ],
)
def test_conflict_types(test_patient, later, expected):
    ingest(test_patient, doc(med()), doc(later, date="2026-02-01"))
    assert [kind for kind, _, _ in kinds(test_patient)] == expected


@pytest.mark.parametrize(
    "note, classification",
    [
        ("increased after review", "intentional_change"),
        ("Dose DECREASED", "intentional_change"),
        ("changed by cardiology", "intentional_change"),
        ("switched brand", "intentional_change"),
        ("stopped then restarted", "intentional_change"),
        ("dose unchanged", "unexplained"),
        ("review pending", "unexplained"),
        ("", "unexplained"),
    ],
)
def test_classification_reads_the_later_note_only(test_patient, note, classification):
    ingest(test_patient, doc(med(note="increased")), doc(med(dose="1000 mg", note=note), date="2026-02-01"))
    assert kinds(test_patient) == [("DOSE_CONFLICT", "metformin", classification)]


@pytest.mark.parametrize(
    "facility, doc_type, expected",
    [
        ("Hospital B", "clinic", ["MISSING"]),
        ("Clinic A", "discharge", ["MISSING"]),
        ("Hospital B", "discharge", ["MISSING"]),
        ("Clinic A", "clinic", []),
    ],
)
def test_missing_needs_a_later_document_of_a_different_facility_or_type(test_patient, facility, doc_type, expected):
    ingest(
        test_patient,
        doc(med(), med(name="Aspirin")),
        doc(med(name="Aspirin"), facility=facility, doc_type=doc_type, date="2026-02-01"),
    )
    assert [kind for kind, _, _ in kinds(test_patient)] == expected


def test_new_medicine_in_a_later_document_is_not_flagged(test_patient):
    ingest(test_patient, doc(med()), doc(med(), med(name="Aspirin"), facility="Hospital B", date="2026-02-01"))
    assert kinds(test_patient) == []


def test_two_later_documents_omitting_a_medicine_give_one_missing_row(test_patient):
    ingest(
        test_patient,
        doc(med(), med(name="Aspirin")),
        doc(med(name="Aspirin"), facility="Hospital B", date="2026-02-01"),
        doc(med(name="Aspirin"), facility="Hospital C", date="2026-03-01"),
    )
    assert kinds(test_patient) == [("MISSING", "metformin", "unexplained")]


def test_missing_points_at_the_most_recent_mention_of_the_medicine(test_patient):
    ingest(
        test_patient,
        doc(med(dose="500 mg"), med(name="Aspirin")),
        doc(med(dose="500 mg"), med(name="Aspirin"), facility="Hospital B", date="2026-02-01"),
        doc(med(name="Aspirin"), facility="Hospital C", date="2026-03-01"),
    )
    rows = query(
        "SELECT d.facility FROM discrepancies x JOIN med_entries e ON e.id = x.entry_a_id"
        " JOIN documents d ON d.id = e.document_id WHERE x.patient_id = ? AND x.type = 'MISSING'",
        test_patient,
    )
    assert [row["facility"] for row in rows] == ["Hospital B"]


def test_two_lines_for_one_medicine_in_a_document_are_not_compared_with_each_other(test_patient):
    ingest(test_patient, doc(med(dose="500 mg"), med(dose="250 mg")))
    assert kinds(test_patient) == []


def test_earlier_and_later_follow_document_date_not_upload_order(test_patient):
    ingest(test_patient, doc(med(dose="1000 mg"), date="2026-03-01"), doc(med(dose="500 mg"), date="2026-01-01"))
    row = query("SELECT entry_a_id, entry_b_id FROM discrepancies WHERE patient_id = ?", test_patient)[0]
    doses = {r["id"]: r["dose"] for r in query("SELECT id, dose FROM med_entries")}
    assert (doses[row["entry_a_id"]], doses[row["entry_b_id"]]) == (500, 1000)


def test_same_document_uploaded_twice_is_stored_once(test_patient):
    first, second = ingest(test_patient, doc(med()), doc(med()))
    assert first == {"document_id": first["document_id"], "duplicate": False}
    assert second == {"document_id": first["document_id"], "duplicate": True}
    assert len(query("SELECT 1 FROM documents WHERE patient_id = ?", test_patient)) == 1


# ----- Endpoints -----


def test_upload_stores_the_text_verbatim_and_compares_the_patient(client, test_patient):
    text = doc(med()) + "A line the parser skips\n"
    response = client.post(f"/patients/{test_patient}/documents", content=text)
    assert response.status_code == 201
    document_id = response.json()["document_id"]
    assert query("SELECT raw_text FROM documents WHERE id = ?", document_id)[0][0] == text

    later = doc(med(dose="1000 mg"), date="2026-02-01")
    response = client.post(f"/patients/{test_patient}/documents", content=later, headers={"Content-Type": "text/plain"})
    assert response.status_code == 201
    assert kinds(test_patient) == [("DOSE_CONFLICT", "metformin", "unexplained")]


def test_uploading_a_seeded_document_again_changes_nothing(client):
    text = query("SELECT raw_text FROM documents WHERE id = 8")[0][0]
    before = (count("documents"), count("med_entries"), count("discrepancies"))
    response = client.post("/patients/4/documents", content=text)
    assert response.status_code == 200
    assert response.json() == {"document_id": 8, "duplicate": True}
    assert (count("documents"), count("med_entries"), count("discrepancies")) == before


@pytest.mark.parametrize(
    "method, url, body, status, code",
    [
        ("POST", "/patients/999/documents", doc(med()).encode(), 404, "NOT_FOUND"),
        ("GET", "/patients/999/compare", None, 404, "NOT_FOUND"),
        ("POST", "/patients/1/documents", b"Facility: X\nNo medicine lines here\n", 422, "INVALID_DOCUMENT"),
        ("POST", "/patients/1/documents", b"", 422, "INVALID_DOCUMENT"),
        ("POST", "/patients/1/documents", b"MED: \xff\xfe", 422, "INVALID_DOCUMENT"),
    ],
)
def test_errors_use_the_agreed_shape_and_store_nothing(client, method, url, body, status, code):
    before = count("documents")
    response = client.request(method, url, content=body)
    assert response.status_code == status
    assert set(response.json()) == {"code", "message", "current_state"}
    assert response.json()["code"] == code
    assert count("documents") == before

"""Step 4, Constraint 12: abstain when a decision-critical fact is missing."""
from contextlib import closing

import pytest

from app import db, gate

A = {"X-Actor": "pharmacist_a"}
UNRESOLVED_TEXT = "Needs verification with your doctor or pharmacist"


def query(sql, *params):
    with closing(db.connect()) as conn:
        return conn.execute(sql, params).fetchall()


def item_of(patient_id, drug):
    return query("SELECT id FROM discrepancies WHERE patient_id = ? AND drug_norm = ?", patient_id, drug)[0][0]


def claim(client, item):
    response = client.post(f"/discrepancies/{item}/claim", headers=A)
    assert response.status_code == 200
    return response.json()["version"]


def add_fact(client, item, name, value="2026-04-20", source="Phone call with Dr Iyer", headers=A):
    return client.post(f"/discrepancies/{item}/facts", json={"name": name, "value": value, "source": source}, headers=headers)


def resolve(client, item, action, version, note="checked"):
    return client.post(f"/discrepancies/{item}/resolve", json={"action": action, "expected_version": version, "note": note}, headers=A)


def detail(client, item):
    response = client.get(f"/discrepancies/{item}")
    assert response.status_code == 200
    return response.json()


def view(client, patient_id):
    response = client.get(f"/patients/{patient_id}/patient-view")
    assert response.status_code == 200
    return {medicine["medicine"]: medicine for medicine in response.json()["medicines"]}


def make_ready(client, item):
    """Claim patient 1's metformin item and add the two facts it needs. Returns the version."""
    claim(client, item)
    add_fact(client, item, "effective_date_b")
    return add_fact(client, item, "change_reason", value="Raised for poor HbA1c").json()["version"]


# ----- T6 to T9 -----


def test_t6_patient_1_abstains_and_accept_is_blocked(client, summary):
    item = item_of(1, "metformin")
    gate_result = detail(client, item)["gate"]
    assert gate_result["decision"] == "ABSTAIN"
    assert gate_result["missing"] == ["effective_date_b", "change_reason"]
    assert gate_result["safe_actions"] == ["request_fact", "escalate", "keep_pending"]
    assert [candidate["action"] for candidate in gate_result["candidate_actions"]] == ["accept_a", "accept_b", "both_valid"]
    assert not any(candidate["selected"] for candidate in gate_result["candidate_actions"])
    assert gate_result["question"] == (
        "Needed for metformin: effective date of the discharge record and reason for the change. "
        "Ask the prescriber whether 1000 mg replaced 500 mg."
    )

    version = claim(client, item)
    response = resolve(client, item, "accept_b", version)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "BLOCKED_MISSING_FACT"
    assert body["current_state"]["missing"] == ["effective_date_b", "change_reason"]
    assert "effective_date_b" in body["message"]
    assert query("SELECT COUNT(*) FROM resolutions")[0][0] == 0
    assert query("SELECT status, version FROM discrepancies WHERE id = ?", item)[0]["status"] == "claimed"
    assert query("SELECT COUNT(*) FROM audit_log WHERE action = 'resolve_blocked'")[0][0] == 1
    summary(f"T6 patient 1: ABSTAIN, missing {gate_result['missing']}, accept_b gives {response.status_code} {body['code']}")


def test_t7_adding_both_facts_makes_it_ready_and_accept_succeeds(client, summary):
    item = item_of(1, "metformin")
    claim(client, item)

    first = add_fact(client, item, "effective_date_b")
    assert first.status_code == 200
    assert first.json()["decision"] == "ABSTAIN"
    assert first.json()["missing"] == ["change_reason"]
    assert first.json()["version"] == 3

    second = add_fact(client, item, "change_reason", value="Raised for poor HbA1c")
    assert second.json()["decision"] == "READY"
    assert second.json()["missing"] == []
    assert second.json()["question"] is None
    assert second.json()["safe_actions"] == ["accept_a", "accept_b", "both_valid", "request_fact", "escalate", "keep_pending"]
    assert not any(candidate["selected"] for candidate in second.json()["candidate_actions"])
    assert second.json()["version"] == 4
    row = query("SELECT gate_decision, missing_facts FROM discrepancies WHERE id = ?", item)[0]
    assert (row["gate_decision"], row["missing_facts"]) == ("READY", "[]")

    response = resolve(client, item, "accept_b", 4)
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"

    rows = query("SELECT actor, payload FROM audit_log WHERE action = 'fact_added' ORDER BY id")
    assert [row["actor"] for row in rows] == ["pharmacist_a", "pharmacist_a"]
    assert '"source": "Phone call with Dr Iyer"' in rows[0]["payload"]
    assert '"name": "effective_date_b"' in rows[0]["payload"]
    assert client.get("/audit/verify").json() == {"valid": True}
    summary(f"T7 patient 1: 2 facts added, READY, accept_b gives {response.status_code} {response.json()['status']}")


def test_t8_verify_passes_then_names_a_tampered_row(client, summary):
    item = item_of(1, "metformin")
    make_ready(client, item)
    assert client.get("/audit/verify").json() == {"valid": True}
    with db.transaction() as conn:
        conn.execute("UPDATE audit_log SET payload = '{\"value\": \"forged\"}' WHERE id = 2")
    result = client.get("/audit/verify").json()
    assert result == {"valid": False, "first_broken_id": 2}
    summary(f"T8 audit: valid, then one row edited directly, verify names row {result['first_broken_id']}")


def test_t9_patient_1_is_unresolved_and_shows_no_dose(client, summary):
    response = client.get("/patients/1/patient-view")
    assert response.json() == {
        "patient_id": 1,
        "medicines": [{"medicine": "metformin", "status": "Unresolved", "message": UNRESOLVED_TEXT}],
    }
    for text in ("500", "1000", "mg", "dose", "stop"):
        assert text not in response.text.lower()
    summary("T9 patient 1 view: Unresolved, no dose in the response")


# ----- The other seeded patients -----


def test_patient_2_missing_medicine_abstains(client):
    gate_result = detail(client, item_of(2, "amlodipine"))["gate"]
    assert gate_result["decision"] == "ABSTAIN"
    assert gate_result["missing"] == ["stop_reason", "current_intake"]
    assert [(c["action"], c["label"]) for c in gate_result["candidate_actions"]] == [("accept_a", "Keep on list"), ("accept_b", "Confirm removed")]
    assert gate_result["question"] == (
        "Needed for amlodipine: why it is absent from the later record and whether the patient is still taking it. "
        "Ask the prescriber and the patient."
    )
    assert "stopped" not in gate_result["question"]


@pytest.mark.parametrize("patient_id, drug", [(3, "atorvastatin"), (4, "telmisartan")])
def test_intentional_changes_with_dates_and_notes_are_ready(client, patient_id, drug):
    gate_result = detail(client, item_of(patient_id, drug))["gate"]
    assert gate_result["decision"] == "READY"
    assert gate_result["missing"] == []
    assert not any(candidate["selected"] for candidate in gate_result["candidate_actions"])


def test_decision_is_stored_when_the_item_is_created(seeded):
    rows = query("SELECT drug_norm, gate_decision, missing_facts FROM discrepancies ORDER BY id")
    assert [tuple(row) for row in rows] == [
        ("metformin", "ABSTAIN", '["effective_date_b", "change_reason"]'),
        ("amlodipine", "ABSTAIN", '["stop_reason", "current_intake"]'),
        ("atorvastatin", "READY", "[]"),
        ("telmisartan", "READY", "[]"),
    ]


# ----- Other rules of the gate -----


def discrepancy_for(lines, entry_b=True, patient=99, kind="DOSE_CONFLICT"):
    """Insert a patient, one document with the given entries, and one discrepancy over them, directly."""
    with db.transaction() as conn:
        conn.execute("INSERT INTO patients (id, name) VALUES (?, 'Test')", (patient,))
        document = conn.execute(
            "INSERT INTO documents (patient_id, doc_type, sha256, raw_text) VALUES (?, 'clinic', 'x', 'x')", (patient,)
        ).lastrowid
        ids = [
            conn.execute(
                "INSERT INTO med_entries (document_id, drug_raw, drug_norm, dose, unit, frequency_per_day, effective_date,"
                " note, source_snippet, confidence) VALUES (?, 'X', 'x', ?, ?, ?, ?, ?, 'MED: X', ?)",
                (document, *line),
            ).lastrowid
            for line in lines
        ]
        item = conn.execute(
            "INSERT INTO discrepancies (patient_id, type, drug_norm, entry_a_id, entry_b_id, fingerprint) VALUES (?, ?, 'x', ?, ?, 'fp')",
            (patient, kind, ids[0], ids[1] if entry_b else None),
        ).lastrowid
    return item


GOOD = (5, "mg", 1, "2026-01-01", "increased", 1.0)


@pytest.mark.parametrize(
    "later, expected",
    [
        (GOOD, []),
        ((None, "mg", 1, "2026-01-01", "n", 1.0), ["dose_b"]),
        ((5, None, 1, "2026-01-01", "n", 1.0), ["unit_b"]),
        ((5, "mg", None, "2026-01-01", "n", 1.0), ["frequency_per_day_b"]),
        ((5, "mg", 1, None, "n", 1.0), ["effective_date_b"]),
        ((5, "mg", 1, "2026-01-01", None, 1.0), ["change_reason"]),
        ((5, "mg", 1, "2026-01-01", "n", 0.79), ["entry_b_confirmed"]),
        ((5, "mg", 1, "2026-01-01", "n", 0.80), []),
    ],
)
def test_null_fields_and_low_confidence_make_it_abstain(seeded, later, expected):
    item = discrepancy_for([GOOD, later])
    with closing(db.connect()) as conn:
        result = gate.evaluate(conn, item)
    assert result["missing"] == expected
    assert result["decision"] == ("ABSTAIN" if expected else "READY")


def test_a_new_type_needs_one_config_line_only(seeded, monkeypatch):
    monkeypatch.setitem(gate.REQUIRED_FACTS, "ALLERGY_CONFLICT", ["allergy_checked"])
    item = discrepancy_for([GOOD, GOOD], kind="ALLERGY_CONFLICT")
    with closing(db.connect()) as conn:
        result = gate.evaluate(conn, item)
    assert result["decision"] == "ABSTAIN"
    assert result["missing"] == ["allergy_checked"]
    assert result["question"] == "Needed for x: allergy checked. Ask the prescriber."


def test_an_unknown_type_fails_closed(seeded):
    item = discrepancy_for([GOOD, GOOD], kind="SOMETHING_NEW")
    with closing(db.connect()) as conn:
        assert gate.evaluate(conn, item)["decision"] == "ABSTAIN"


@pytest.mark.parametrize("action", ["request_fact", "escalate", "keep_pending"])
def test_safe_actions_are_allowed_while_abstaining_and_keep_the_claim(client, action):
    item = item_of(1, "metformin")
    version = claim(client, item)
    response = resolve(client, item, action, version, note="waiting")
    assert response.status_code == 200
    data = detail(client, item)
    assert data["status"] == "claimed"
    assert data["resolutions"][0]["note"] == "waiting"
    assert data["gate"]["decision"] == "ABSTAIN"


def test_both_valid_does_not_apply_to_a_missing_medicine(client):
    item = item_of(2, "amlodipine")
    claim(client, item)
    add_fact(client, item, "stop_reason", value="Not known")
    version = add_fact(client, item, "current_intake", value="Still taking").json()["version"]
    assert resolve(client, item, "both_valid", version).status_code == 422
    assert detail(client, item)["status"] == "claimed"
    assert resolve(client, item, "accept_a", version).status_code == 200


def test_only_the_claim_holder_can_add_a_fact(client):
    item = item_of(1, "metformin")
    assert add_fact(client, item, "effective_date_b").json()["code"] == "NOT_CLAIMED"
    claim(client, item)
    response = add_fact(client, item, "effective_date_b", headers={"X-Actor": "pharmacist_b"})
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_CLAIMED"
    assert query("SELECT COUNT(*) FROM facts")[0][0] == 0
    assert query("SELECT COUNT(*) FROM audit_log WHERE action = 'fact_rejected'")[0][0] == 2


@pytest.mark.parametrize(
    "fact",
    [
        {"name": "effective_date_b", "value": "2026-04-20"},
        {"name": "effective_date_b", "value": "2026-04-20", "source": "  "},
        {"name": "", "value": "x", "source": "y"},
        {"name": "effective_date_b", "value": "soon", "source": "y"},
        {"name": "effective_date_b", "value": "2026-02-30", "source": "y"},
    ],
)
def test_a_fact_needs_a_name_a_value_and_a_source(client, fact):
    item = item_of(1, "metformin")
    version = claim(client, item)
    response = client.post(f"/discrepancies/{item}/facts", json=fact, headers=A)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    assert detail(client, item)["version"] == version
    assert detail(client, item)["facts"] == []


def test_an_unrelated_fact_changes_nothing_at_the_gate(client):
    item = item_of(1, "metformin")
    claim(client, item)
    response = add_fact(client, item, "weather", value="sunny")
    assert response.json()["decision"] == "ABSTAIN"
    assert response.json()["missing"] == ["effective_date_b", "change_reason"]


def test_a_fact_cannot_be_added_after_resolving(client):
    item = item_of(1, "metformin")
    version = make_ready(client, item)
    resolve(client, item, "accept_b", version)
    assert add_fact(client, item, "note", value="late").status_code == 409


def test_source_records_are_never_changed_by_a_fact(client):
    item = item_of(1, "metformin")
    before = [tuple(row) for row in query("SELECT * FROM med_entries")]
    make_ready(client, item)
    assert [tuple(row) for row in query("SELECT * FROM med_entries")] == before
    assert detail(client, item)["entry_b"]["effective_date"] is None
    assert detail(client, item)["entry_b"]["source_snippet"] == "MED: Metformin | 1000 mg | twice daily | oral |  |"


def test_queue_lists_every_item_with_its_gate_and_question(client):
    items = client.get("/queue").json()
    assert [(item["patient_name"], item["drug_norm"], item["gate_decision"]) for item in items] == [
        ("Ravi", "metformin", "ABSTAIN"),
        ("Meera", "amlodipine", "ABSTAIN"),
        ("Arjun", "atorvastatin", "READY"),
        ("Sunita", "telmisartan", "READY"),
    ]
    assert items[0]["missing"] == ["effective_date_b", "change_reason"]
    assert items[0]["question"].startswith("Needed for metformin")
    assert items[2]["question"] is None


def test_detail_returns_both_entries_facts_and_resolutions(client):
    item = item_of(3, "atorvastatin")
    data = detail(client, item)
    assert data["entry_a"]["source_snippet"] == "MED: Atorvastatin | 10 mg | once daily | oral | 2026-01-15 |"
    assert data["entry_b"]["source_snippet"].endswith("increased after LDL review")
    assert data["facts"] == []
    assert data["resolutions"] == []
    assert client.get("/discrepancies/999").json()["code"] == "NOT_FOUND"


# ----- Patient view -----


def test_patient_2_view_hides_only_the_unresolved_medicine(client):
    medicines = view(client, 2)
    assert medicines["amlodipine"] == {"medicine": "amlodipine", "status": "Unresolved", "message": UNRESOLVED_TEXT}
    assert medicines["pantoprazole"]["status"] == "Verified"
    assert medicines["pantoprazole"]["records"][0]["dose"] == 40


def test_pending_shows_every_record_without_choosing(client):
    medicines = view(client, 3)
    assert medicines["atorvastatin"]["status"] == "Pending"
    assert [record["dose"] for record in medicines["atorvastatin"]["records"]] == [10, 20]


def test_no_discrepancy_is_verified_with_the_original_names(client):
    metformin = view(client, 3)["metformin"]
    assert metformin["status"] == "Verified"
    assert [record["name"] for record in metformin["records"]] == ["Glycomet", "Metformin"]


@pytest.mark.parametrize("action, doses", [("accept_a", [40]), ("accept_b", [80]), ("both_valid", [40, 80])])
def test_resolved_shows_what_the_reviewer_accepted(client, action, doses):
    item = item_of(4, "telmisartan")
    version = claim(client, item)
    assert view(client, 4)["telmisartan"]["status"] == "Pending"
    resolve(client, item, action, version)
    telmisartan = view(client, 4)["telmisartan"]
    assert telmisartan["status"] == "Verified"
    assert [record["dose"] for record in telmisartan["records"]] == doses


def test_patient_1_moves_from_unresolved_to_pending_to_verified(client):
    item = item_of(1, "metformin")
    assert view(client, 1)["metformin"]["status"] == "Unresolved"
    version = make_ready(client, item)
    assert view(client, 1)["metformin"]["status"] == "Pending"
    resolve(client, item, "accept_b", version)
    metformin = view(client, 1)["metformin"]
    assert metformin["status"] == "Verified"
    assert [record["dose"] for record in metformin["records"]] == [1000]


def test_confirmed_removal_shows_no_dose_and_no_instruction(client):
    item = item_of(2, "amlodipine")
    claim(client, item)
    add_fact(client, item, "stop_reason", value="Prescriber confirmed")
    version = add_fact(client, item, "current_intake", value="Not taking").json()["version"]
    resolve(client, item, "accept_b", version)
    response = client.get("/patients/2/patient-view")
    amlodipine = view(client, 2)["amlodipine"]
    assert amlodipine["status"] == "Verified"
    assert amlodipine["records"] == []
    assert "stop" not in response.text.lower()


def test_a_single_unreadable_entry_is_unresolved_not_verified(client):
    with db.transaction() as conn:
        conn.execute("INSERT INTO patients (id, name) VALUES (99, 'Test')")
    text = "Facility: X\nType: clinic\nDate: 2026-01-01\nMED: Aspirin | one tablet | OD | oral | 2026-01-01 |\nMED: Foo Bar | 5 mg | OD | oral | 2026-01-01 |\n"
    assert client.post("/patients/99/documents", content=text).status_code == 201
    medicines = view(client, 99)
    assert medicines["aspirin"]["status"] == "Unresolved"
    assert medicines["foo bar"]["status"] == "Unresolved"


def test_unknown_patient_view_is_a_404(client):
    assert client.get("/patients/999/patient-view").json()["code"] == "NOT_FOUND"

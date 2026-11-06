"""Step 3, Constraint 9: simultaneous claims, retries and duplicate submissions, on a real server."""
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app import audit, db
from app.idempotency import RequestInfo, run_idempotent

# Same medicine and dose as patient 4's latest document, so uploading it adds no discrepancy.
NEW_DOCUMENT = (
    "Facility: City Hospital\nType: discharge\nDate: 2026-07-01\nPrescriber: Dr Iyer\n"
    "MED: Telmisartan | 80 mg | once daily | oral | 2026-07-01 | dose increased per cardiology review\n"
)


def overlap(server, count, send, second_server=None):
    """Call send(client, index) from threads released together by a barrier, each on an open connection.

    With a second server, odd-numbered threads use it. Returns the responses in order, and how
    many requests had started before the first one finished.
    """
    barrier = threading.Barrier(count)
    spans = [None] * count

    def run(index):
        target = second_server if second_server and index % 2 else server
        with target.client() as http_client:
            http_client.get("/openapi.json")
            barrier.wait()
            started = time.perf_counter()
            response = send(http_client, index)
            spans[index] = (started, time.perf_counter())
            return response

    with ThreadPoolExecutor(count) as pool:
        responses = list(pool.map(run, range(count)))
    first_finished = min(finished for _, finished in spans)
    in_flight = sum(started < first_finished for started, _ in spans)
    assert in_flight >= 2, "the requests did not overlap"
    return responses, in_flight


def query(sql, *params):
    with closing(db.connect()) as conn:
        return conn.execute(sql, params).fetchall()


def scalar(sql, *params):
    return query(sql, *params)[0][0]


def item_of(patient_id):
    return scalar("SELECT id FROM discrepancies WHERE patient_id = ?", patient_id)


def state(item):
    row = query("SELECT status, version, claimed_by FROM discrepancies WHERE id = ?", item)[0]
    return dict(row)


def resolutions(item):
    return scalar("SELECT COUNT(*) FROM resolutions WHERE discrepancy_id = ?", item)


def audit_counts():
    return Counter(row["action"] for row in query("SELECT action FROM audit_log"))


def claim(http_client, item, actor="pharmacist_a", key=None):
    headers = {"X-Actor": actor}
    if key:
        headers["Idempotency-Key"] = key
    return http_client.post(f"/discrepancies/{item}/claim", headers=headers)


def resolve(http_client, item, action="accept_b", expected_version=2, note="checked", actor="pharmacist_a", key=None):
    headers = {"X-Actor": actor}
    if key:
        headers["Idempotency-Key"] = key
    body = {"action": action, "expected_version": expected_version, "note": note}
    return http_client.post(f"/discrepancies/{item}/resolve", json=body, headers=headers)


@pytest.fixture
def claimed_item(client):
    """Patient 4's discrepancy, claimed by pharmacist_a, so it is at version 2."""
    item = item_of(4)
    assert claim(client, item, key="setup-claim").status_code == 200
    return item


# ----- T1 to T5 -----


def test_t1_twenty_simultaneous_claims_have_exactly_one_winner(server, client, summary):
    item = item_of(4)
    actors = [f"user_{number}" for number in range(1, 21)]
    responses, in_flight = overlap(server, 20, lambda http_client, index: claim(http_client, item, actors[index]))

    codes = Counter(response.status_code for response in responses)
    assert codes == {200: 1, 409: 19}
    winner = next(actor for actor, response in zip(actors, responses) if response.status_code == 200)
    assert state(item) == {"status": "claimed", "version": 2, "claimed_by": winner}
    for actor, response in zip(actors, responses):
        if actor == winner:
            assert response.json() == {"claimed_by": winner, "version": 2}
        else:
            assert response.json()["code"] == "CLAIMED_BY_OTHER"
            assert response.json()["current_state"]["claimed_by"] == winner
    counts = audit_counts()
    assert counts["claim"] == 1
    assert counts["claim_rejected"] == 19
    assert client.get("/audit/verify").json() == {"valid": True}
    summary(
        f"T1 20 simultaneous claims: {codes[200]} x 200, {codes[409]} x 409, winner {winner}, audit "
        f"{counts['claim']} claim + {counts['claim_rejected']} claim_rejected, {in_flight} of 20 in flight at the first finish"
    )


def test_t2_same_resolve_with_same_key_ten_times_runs_once(server, claimed_item, summary):
    responses, in_flight = overlap(server, 10, lambda http_client, _: resolve(http_client, claimed_item, key="key-t2"))

    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    replays = sum(response.headers.get("Idempotent-Replay") == "true" for response in responses)
    assert replays == 9
    assert resolutions(claimed_item) == 1
    assert state(claimed_item) == {"status": "resolved", "version": 3, "claimed_by": "pharmacist_a"}
    assert audit_counts()["resolve"] == 1
    summary(
        f"T2 same resolve and key x 10: {len({r.content for r in responses})} distinct response, {replays} replays, "
        f"{resolutions(claimed_item)} resolutions row, {in_flight} of 10 in flight at the first finish"
    )


def test_t3_same_resolve_with_no_key_five_times_runs_once(server, claimed_item, summary):
    responses, in_flight = overlap(server, 5, lambda http_client, _: resolve(http_client, claimed_item))

    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    assert resolutions(claimed_item) == 1
    assert state(claimed_item)["version"] == 3
    summary(f"T3 same resolve, no key x 5: {resolutions(claimed_item)} resolutions row, {in_flight} of 5 in flight at the first finish")


def test_t4_same_document_uploaded_five_times_is_stored_once(server, client, summary):
    documents, discrepancies = scalar("SELECT COUNT(*) FROM documents"), scalar("SELECT COUNT(*) FROM discrepancies")
    responses, in_flight = overlap(
        server, 5, lambda http_client, _: http_client.post("/patients/4/documents", content=NEW_DOCUMENT, headers={"X-Actor": "clinician"})
    )

    assert {response.status_code for response in responses} == {201}
    assert len({response.content for response in responses}) == 1
    assert scalar("SELECT COUNT(*) FROM documents") == documents + 1
    assert scalar("SELECT COUNT(*) FROM documents WHERE raw_text = ?", NEW_DOCUMENT) == 1
    assert scalar("SELECT COUNT(*) FROM discrepancies") == discrepancies
    summary(
        f"T4 same document x 5: {scalar('SELECT COUNT(*) FROM documents WHERE raw_text = ?', NEW_DOCUMENT)} document row, "
        f"discrepancies {discrepancies} before and {scalar('SELECT COUNT(*) FROM discrepancies')} after, {in_flight} of 5 in flight at the first finish"
    )


def test_t4_same_document_from_different_actors_is_also_stored_once(server, client):
    documents, discrepancies = scalar("SELECT COUNT(*) FROM documents"), scalar("SELECT COUNT(*) FROM discrepancies")
    responses, _ = overlap(
        server, 5, lambda http_client, index: http_client.post("/patients/4/documents", content=NEW_DOCUMENT, headers={"X-Actor": f"user_{index}"})
    )

    assert Counter(response.status_code for response in responses) == {201: 1, 200: 4}
    assert len({response.json()["document_id"] for response in responses}) == 1
    assert scalar("SELECT COUNT(*) FROM documents") == documents + 1
    assert scalar("SELECT COUNT(*) FROM discrepancies") == discrepancies


def test_t5_resolve_with_an_old_version_changes_nothing(client, claimed_item, summary):
    before = state(claimed_item)
    response = resolve(client, claimed_item, expected_version=1)

    assert response.status_code == 409
    assert response.json()["code"] == "STALE_VERSION"
    assert response.json()["current_state"] == before
    assert state(claimed_item) == before
    assert resolutions(claimed_item) == 0
    assert audit_counts()["resolve_rejected"] == 1
    summary(f"T5 old version: {response.status_code} {response.json()['code']}, {resolutions(claimed_item)} resolutions rows, version still {state(claimed_item)['version']}")


# ----- The same guarantees across two server processes, where only SQLite can protect the data -----


def test_t1_holds_across_two_server_processes(server, second_server, client, summary):
    item = item_of(4)
    actors = [f"user_{number}" for number in range(1, 21)]
    responses, in_flight = overlap(server, 20, lambda http_client, index: claim(http_client, item, actors[index]), second_server)

    codes = Counter(response.status_code for response in responses)
    assert codes == {200: 1, 409: 19}
    winner = next(actor for actor, response in zip(actors, responses) if response.status_code == 200)
    assert state(item) == {"status": "claimed", "version": 2, "claimed_by": winner}
    counts = audit_counts()
    assert counts["claim"] == 1
    assert counts["claim_rejected"] == 19
    assert client.get("/audit/verify").json() == {"valid": True}
    summary(f"T1 across 2 server processes: {codes[200]} x 200, {codes[409]} x 409, {in_flight} of 20 in flight at the first finish")


def test_t2_holds_across_two_server_processes(server, second_server, claimed_item, summary):
    responses, in_flight = overlap(
        server, 10, lambda http_client, _: resolve(http_client, claimed_item, key="key-two-processes"), second_server
    )

    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    assert resolutions(claimed_item) == 1
    assert state(claimed_item)["version"] == 3
    summary(f"T2 across 2 server processes: {resolutions(claimed_item)} resolutions row, {in_flight} of 10 in flight at the first finish")


# ----- More of the same rules -----


def test_simultaneous_resolves_with_different_keys_have_one_winner(server, claimed_item):
    responses, _ = overlap(server, 10, lambda http_client, index: resolve(http_client, claimed_item, key=f"key-{index}"))

    assert Counter(response.status_code for response in responses) == {200: 1, 409: 9}
    assert {response.json()["code"] for response in responses if response.status_code == 409} == {"STALE_VERSION"}
    assert resolutions(claimed_item) == 1
    assert state(claimed_item)["version"] == 3


def test_key_reused_with_a_different_body_is_rejected(client, claimed_item):
    assert resolve(client, claimed_item, action="keep_pending", key="key-a").status_code == 200
    before = state(claimed_item)

    response = resolve(client, claimed_item, action="escalate", expected_version=3, key="key-a")

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert state(claimed_item) == before
    assert resolutions(claimed_item) == 1
    assert audit_counts()["idempotency_key_reused"] == 1


def test_key_reused_on_a_different_item_is_rejected(client):
    assert claim(client, item_of(4), key="key-a").status_code == 200
    response = claim(client, item_of(3), key="key-a")
    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert state(item_of(3))["status"] == "open"


def test_the_same_key_from_two_actors_is_two_requests(client):
    item = item_of(4)
    assert claim(client, item, "pharmacist_a", key="shared").status_code == 200
    response = claim(client, item, "pharmacist_b", key="shared")
    assert response.status_code == 409
    assert response.json()["code"] == "CLAIMED_BY_OTHER"
    assert "Idempotent-Replay" not in response.headers


def test_claiming_again_with_a_new_key_says_already_yours(client, claimed_item):
    response = claim(client, claimed_item, key="another-click")
    assert response.status_code == 200
    assert response.json() == {"claimed_by": "pharmacist_a", "version": 2, "message": "already yours"}
    assert state(claimed_item)["version"] == 2
    assert "Idempotent-Replay" not in response.headers


def test_retry_with_no_key_replays_the_first_result(client):
    item = item_of(4)
    first, second = claim(client, item), claim(client, item)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert "Idempotent-Replay" not in first.headers
    assert second.headers["Idempotent-Replay"] == "true"
    assert state(item)["version"] == 2


def test_a_rejected_attempt_is_stored_and_replayed_too(client):
    item = item_of(4)
    claim(client, item, "pharmacist_a")
    first, second = claim(client, item, "pharmacist_b"), claim(client, item, "pharmacist_b")
    assert first.status_code == second.status_code == 409
    assert first.content == second.content
    assert second.headers["Idempotent-Replay"] == "true"
    counts = audit_counts()
    assert counts["claim_rejected"] == 1
    assert counts["idempotent_replay"] == 1


@pytest.mark.parametrize("claim_first", [False, True])
def test_only_the_claim_holder_can_resolve(client, claim_first):
    item = item_of(4)
    if claim_first:
        claim(client, item, "pharmacist_a")
    before = state(item)
    response = resolve(client, item, actor="pharmacist_b", expected_version=before["version"])
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_CLAIMED"
    assert response.json()["current_state"] == before
    assert state(item) == before
    assert resolutions(item) == 0
    assert audit_counts()["resolve_rejected"] == 1


def test_a_resolved_item_cannot_be_resolved_again(client, claimed_item):
    assert resolve(client, claimed_item, expected_version=2).status_code == 200
    response = resolve(client, claimed_item, action="both_valid", expected_version=3)
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_CLAIMED"
    assert resolutions(claimed_item) == 1


def test_a_resolved_item_cannot_be_claimed(client, claimed_item):
    resolve(client, claimed_item, expected_version=2)
    response = claim(client, claimed_item, "pharmacist_b")
    assert response.status_code == 409
    assert response.json()["code"] == "CLAIMED_BY_OTHER"
    assert response.json()["current_state"]["status"] == "resolved"


@pytest.mark.parametrize(
    "action, status",
    [
        ("accept_a", "resolved"),
        ("accept_b", "resolved"),
        ("both_valid", "resolved"),
        ("request_fact", "claimed"),
        ("escalate", "claimed"),
        ("keep_pending", "claimed"),
    ],
)
def test_each_action_records_a_row_and_bumps_the_version(client, claimed_item, action, status):
    response = resolve(client, claimed_item, action=action, note="the note")
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert state(claimed_item) == {"status": status, "version": 3, "claimed_by": "pharmacist_a"}
    row = query("SELECT action, actor, note, created_at FROM resolutions WHERE discrepancy_id = ?", claimed_item)[0]
    assert (row["action"], row["actor"], row["note"]) == (action, "pharmacist_a", "the note")
    assert row["created_at"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"expected_version": 2}',
        b'{"action": "stop_medicine", "expected_version": 2}',
        b'{"action": ["accept_a"], "expected_version": 2}',
        b'{"action": "accept_a"}',
        b'{"action": "accept_a", "expected_version": "2"}',
        b'{"action": "accept_a", "expected_version": true}',
        b'{"action": "accept_a", "expected_version": 2, "note": 5}',
        b"\xff\xfe",
    ],
)
def test_an_invalid_resolve_body_changes_nothing(client, claimed_item, body):
    before = state(claimed_item)
    response = client.post(f"/discrepancies/{claimed_item}/resolve", content=body)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    assert state(claimed_item) == before
    assert resolutions(claimed_item) == 0
    assert audit_counts()["resolve_rejected"] == 1


def test_a_request_with_no_actor_is_rejected(server, seeded):
    item = item_of(4)
    with server.client() as http_client:
        response = http_client.post(f"/discrepancies/{item}/claim")
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_ACTOR"
    assert state(item)["status"] == "open"


def test_an_unknown_item_is_a_404_with_the_agreed_shape(client):
    for response in (claim(client, 999), resolve(client, 999)):
        assert response.status_code == 404
        assert set(response.json()) == {"code", "message", "current_state"}
        assert response.json()["code"] == "NOT_FOUND"


# ----- Audit chain -----


def test_every_attempt_is_audited_and_the_chain_stays_valid(client, claimed_item):
    claim(client, claimed_item, "pharmacist_b")
    resolve(client, claimed_item, expected_version=1)
    resolve(client, claimed_item, key="done")
    resolve(client, claimed_item, key="done")
    client.post("/patients/4/documents", content=NEW_DOCUMENT)
    client.post("/patients/4/documents", content="no medicine lines")

    rows = query("SELECT actor, action, entity FROM audit_log ORDER BY id")
    assert [(row["actor"], row["action"]) for row in rows] == [
        ("pharmacist_a", "claim"),
        ("pharmacist_b", "claim_rejected"),
        ("pharmacist_a", "resolve_rejected"),
        ("pharmacist_a", "resolve"),
        ("pharmacist_a", "idempotent_replay"),
        ("pharmacist_a", "upload"),
        ("pharmacist_a", "upload_rejected"),
    ]
    assert rows[0]["entity"] == f"discrepancy:{claimed_item}"
    assert client.get("/audit/verify").json() == {"valid": True}


def test_an_empty_audit_log_is_valid(client):
    assert client.get("/audit/verify").json() == {"valid": True}


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE audit_log SET payload = '{}' WHERE id = 2",
        "UPDATE audit_log SET actor = 'someone_else' WHERE id = 2",
        "UPDATE audit_log SET hash = 'abc' WHERE id = 2",
    ],
)
def test_verify_names_the_first_tampered_row(client, tamper):
    for actor in ("user_1", "user_2", "user_3"):
        claim(client, item_of(4), actor)
    with db.transaction() as conn:
        conn.execute(tamper)
    assert client.get("/audit/verify").json() == {"valid": False, "first_broken_id": 2}


def test_verify_notices_a_deleted_row(client):
    for actor in ("user_1", "user_2", "user_3"):
        claim(client, item_of(4), actor)
    with db.transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE id = 2")
    assert client.get("/audit/verify").json() == {"valid": False, "first_broken_id": 3}


def test_audit_append_refuses_to_run_outside_a_transaction(seeded):
    with closing(db.connect()) as conn:
        with pytest.raises(RuntimeError):
            audit.append(conn, "pharmacist_a", "claim", "discrepancy:1", {})
    assert query("SELECT COUNT(*) FROM audit_log")[0][0] == 0


def test_a_failed_handler_rolls_back_its_write_audit_and_key(seeded):
    item = item_of(4)
    info = RequestInfo("pharmacist_a", "POST /test", item, f"discrepancy:{item}", b"", "key-fail")

    def failing_handler(conn):
        conn.execute("UPDATE discrepancies SET version = 99 WHERE id = ?", (item,))
        audit.append(conn, "pharmacist_a", "resolve", f"discrepancy:{item}", {})
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_idempotent(info, failing_handler)
    assert state(item)["version"] == 1
    assert scalar("SELECT COUNT(*) FROM audit_log") == 0
    assert scalar("SELECT COUNT(*) FROM idempotency") == 0


# ----- Reset -----


def test_reset_restores_the_seed_and_forgets_keys_and_audit(client, claimed_item):
    resolve(client, claimed_item, key="key-before-reset")
    response = client.post("/admin/reset")
    assert response.status_code == 200
    item = item_of(4)
    assert state(item) == {"status": "open", "version": 1, "claimed_by": None}
    assert scalar("SELECT COUNT(*) FROM audit_log") == 0
    assert scalar("SELECT COUNT(*) FROM idempotency") == 0
    assert scalar("SELECT COUNT(*) FROM resolutions") == 0
    assert scalar("SELECT COUNT(*) FROM documents") == 8
    assert claim(client, item, key="setup-claim").status_code == 200

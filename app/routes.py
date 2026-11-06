"""HTTP endpoints. Handlers are plain def, so each request runs in its own thread."""
import json
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import seed
from app import audit
from app.compare import compare_report
from app.db import connect
from app.extract import ingest_document, parse_document
from app.idempotency import RequestInfo, error_body, run_idempotent

router = APIRouter()

ACTIONS = ("accept_a", "accept_b", "both_valid", "request_fact", "escalate", "keep_pending")
RESOLVING_ACTIONS = ("accept_a", "accept_b", "both_valid")


def error(status_code, code, message, current_state=None):
    return JSONResponse(status_code=status_code, content=error_body(code, message, current_state))


def patient_exists(conn, patient_id):
    return conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is not None


def current_state(conn, discrepancy_id):
    row = conn.execute(
        "SELECT status, version, claimed_by FROM discrepancies WHERE id = ?", (discrepancy_id,)
    ).fetchone()
    return dict(row) if row else None


async def read_body(request: Request):
    """Async only to read the bytes. Any content type works, so plain text is never parsed as JSON."""
    return await request.body()


def run_write(request, endpoint, resource_id, entity, body, handler):
    """Every write comes through here: one BEGIN IMMEDIATE transaction, idempotent, with its audit rows."""
    actor = request.headers.get("X-Actor", "").strip()
    if not actor:
        return error(400, "MISSING_ACTOR", "Send the X-Actor header.")
    info = RequestInfo(actor, endpoint, resource_id, entity, body, request.headers.get("Idempotency-Key") or None)
    return run_idempotent(info, lambda conn: handler(conn, actor))


# ----- Documents -----


def store_document(conn, actor, patient_id, body):
    def reject(status_code, code, message):
        audit.append(conn, actor, "upload_rejected", f"patient:{patient_id}", {"code": code})
        return status_code, error_body(code, message)

    try:
        raw_text = body.decode("utf-8")
    except UnicodeDecodeError:
        return reject(422, "INVALID_DOCUMENT", "The document must be UTF-8 text.")
    if not parse_document(raw_text)[1]:
        return reject(422, "INVALID_DOCUMENT", "No MED lines found, so nothing was stored.")
    if not patient_exists(conn, patient_id):
        return reject(404, "NOT_FOUND", f"Patient {patient_id} does not exist.")
    result = ingest_document(conn, patient_id, raw_text)
    action = "upload_duplicate" if result["duplicate"] else "upload"
    audit.append(conn, actor, action, f"document:{result['document_id']}", {"patient_id": patient_id})
    return (200 if result["duplicate"] else 201), result


@router.post("/patients/{patient_id}/documents")
def upload_document(patient_id: int, request: Request, body: bytes = Depends(read_body)):
    return run_write(
        request,
        "POST /patients/{id}/documents",
        patient_id,
        f"patient:{patient_id}",
        body,
        lambda conn, actor: store_document(conn, actor, patient_id, body),
    )


@router.get("/patients/{patient_id}/compare")
def get_compare(patient_id: int):
    with closing(connect()) as conn:
        if not patient_exists(conn, patient_id):
            return error(404, "NOT_FOUND", f"Patient {patient_id} does not exist.")
        return compare_report(conn, patient_id)


# ----- Claim -----


def claim_discrepancy(conn, actor, discrepancy_id):
    entity = f"discrepancy:{discrepancy_id}"
    changed = conn.execute(
        "UPDATE discrepancies SET status = 'claimed', claimed_by = ?, version = version + 1"
        " WHERE id = ? AND status = 'open'",
        (actor, discrepancy_id),
    ).rowcount
    state = current_state(conn, discrepancy_id)
    if changed == 1:
        audit.append(conn, actor, "claim", entity, state)
        return 200, {"claimed_by": actor, "version": state["version"]}
    if state is None:
        audit.append(conn, actor, "claim_rejected", entity, {"code": "NOT_FOUND"})
        return 404, error_body("NOT_FOUND", f"Discrepancy {discrepancy_id} does not exist.")
    if state["claimed_by"] == actor:
        audit.append(conn, actor, "claim_already_yours", entity, state)
        return 200, {"claimed_by": actor, "version": state["version"], "message": "already yours"}
    audit.append(conn, actor, "claim_rejected", entity, {"code": "CLAIMED_BY_OTHER", "current_state": state})
    return 409, error_body("CLAIMED_BY_OTHER", f"Already claimed by {state['claimed_by']}.", state)


@router.post("/discrepancies/{discrepancy_id}/claim")
def claim(discrepancy_id: int, request: Request, body: bytes = Depends(read_body)):
    return run_write(
        request,
        "POST /discrepancies/{id}/claim",
        discrepancy_id,
        f"discrepancy:{discrepancy_id}",
        body,
        lambda conn, actor: claim_discrepancy(conn, actor, discrepancy_id),
    )


# ----- Resolve -----


def parse_resolve_body(body):
    """Return (action, expected_version, note), or None if the body is not a valid request."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    action, expected_version, note = data.get("action"), data.get("expected_version"), data.get("note")
    if not isinstance(action, str) or action not in ACTIONS:
        return None
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        return None
    if note is not None and not isinstance(note, str):
        return None
    return action, expected_version, note


def resolve_discrepancy(conn, actor, discrepancy_id, body):
    entity = f"discrepancy:{discrepancy_id}"
    state = current_state(conn, discrepancy_id)

    def reject(status_code, code, message):
        audit.append(conn, actor, "resolve_rejected", entity, {"code": code, "current_state": state})
        return status_code, error_body(code, message, state)

    parsed = parse_resolve_body(body)
    if parsed is None:
        actions = ", ".join(ACTIONS)
        return reject(422, "INVALID_REQUEST", f"Send JSON with action ({actions}), expected_version and an optional note.")
    action, expected_version, note = parsed
    if state is None:
        return reject(404, "NOT_FOUND", f"Discrepancy {discrepancy_id} does not exist.")
    if state["claimed_by"] != actor:
        return reject(409, "NOT_CLAIMED", "Claim this item before resolving it.")
    if expected_version != state["version"]:
        return reject(409, "STALE_VERSION", f"This item is at version {state['version']}, not {expected_version}.")
    if state["status"] != "claimed":
        return reject(409, "NOT_CLAIMED", "This item is already resolved.")

    resolution_id = conn.execute(
        "INSERT INTO resolutions (discrepancy_id, action, actor, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (discrepancy_id, action, actor, note, datetime.now(timezone.utc).isoformat()),
    ).lastrowid
    status = "resolved" if action in RESOLVING_ACTIONS else "claimed"
    conn.execute("UPDATE discrepancies SET status = ?, version = version + 1 WHERE id = ?", (status, discrepancy_id))
    version = state["version"] + 1
    audit.append(conn, actor, "resolve", entity, {"action": action, "note": note, "status": status, "version": version})
    return 200, {"resolution_id": resolution_id, "action": action, "status": status, "version": version}


@router.post("/discrepancies/{discrepancy_id}/resolve")
def resolve(discrepancy_id: int, request: Request, body: bytes = Depends(read_body)):
    return run_write(
        request,
        "POST /discrepancies/{id}/resolve",
        discrepancy_id,
        f"discrepancy:{discrepancy_id}",
        body,
        lambda conn, actor: resolve_discrepancy(conn, actor, discrepancy_id, body),
    )


# ----- Audit and admin -----


@router.get("/audit/verify")
def audit_verify():
    with closing(connect()) as conn:
        return audit.verify(conn)


@router.post("/admin/reset")
def admin_reset():
    seed.reset()
    return {"status": "reset"}

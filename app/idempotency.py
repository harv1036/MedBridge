"""Exactly-once writes: a request's result is stored in the same transaction as the write it made."""
import hashlib
import json
from dataclasses import dataclass

from fastapi import Response

from app import audit
from app.db import transaction


@dataclass
class RequestInfo:
    actor: str
    endpoint: str  # the route without the id, such as "POST /discrepancies/{id}/claim"
    resource_id: int
    entity: str  # how the audit log names the resource, such as "discrepancy:5"
    body: bytes
    key: str | None = None  # the Idempotency-Key header, if one was sent


def error_body(code, message, current_state=None):
    return {"code": code, "message": message, "current_state": current_state}


def run_idempotent(request_info, handler):
    """Run handler(conn) once per key, returning its (status_code, body) as a response.

    BEGIN IMMEDIATE makes the lookup and the write one atomic step, so overlapping
    retries wait their turn and then find the stored result instead of running again.
    """
    info = request_info
    request_hash = hashlib.sha256(f"{info.resource_id}|".encode("utf-8") + info.body).hexdigest()
    key = info.key or hashlib.sha256(f"{info.actor}|{info.endpoint}|{info.resource_id}|".encode("utf-8") + info.body).hexdigest()
    headers = {}
    with transaction() as conn:
        stored = conn.execute(
            'SELECT request_hash, status_code, response_json FROM idempotency WHERE "key" = ? AND endpoint = ? AND actor = ?',
            (key, info.endpoint, info.actor),
        ).fetchone()
        if stored is None:
            status_code, body = handler(conn)
            content = json.dumps(body)
            conn.execute(
                'INSERT INTO idempotency ("key", endpoint, actor, request_hash, status_code, response_json)'
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, info.endpoint, info.actor, request_hash, status_code, content),
            )
        elif stored["request_hash"] == request_hash:
            status_code, content = stored["status_code"], stored["response_json"]
            headers["Idempotent-Replay"] = "true"
            audit.append(conn, info.actor, "idempotent_replay", info.entity, {"endpoint": info.endpoint, "status_code": status_code})
        else:
            status_code = 422
            content = json.dumps(error_body("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used for a different request."))
            audit.append(conn, info.actor, "idempotency_key_reused", info.entity, {"endpoint": info.endpoint})
    return Response(content=content, status_code=status_code, media_type="application/json", headers=headers)

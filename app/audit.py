"""Append-only audit log. Each row's hash covers the previous row's hash, so any edit breaks the chain."""
import hashlib
import json
from datetime import datetime, timezone

GENESIS_HASH = "0" * 64


def row_hash(prev_hash, actor, action, entity, payload, ts):
    return hashlib.sha256((prev_hash + actor + action + entity + payload + ts).encode("utf-8")).hexdigest()


def append(conn, actor, action, entity, payload):
    """Add one row inside the caller's open transaction, so it commits or rolls back with the write it records."""
    if not conn.in_transaction:
        raise RuntimeError("audit.append must run inside an open transaction")
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else GENESIS_HASH
    ts = datetime.now(timezone.utc).isoformat()
    payload_text = json.dumps(payload, sort_keys=True)
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, entity, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, actor, action, entity, payload_text, prev_hash, row_hash(prev_hash, actor, action, entity, payload_text, ts)),
    )


def verify(conn):
    """Recompute the chain. Returns {valid: true} or {valid: false, first_broken_id}."""
    prev_hash = GENESIS_HASH
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = row_hash(prev_hash, row["actor"], row["action"], row["entity"], row["payload"], row["ts"])
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return {"valid": False, "first_broken_id": row["id"]}
        prev_hash = row["hash"]
    return {"valid": True}

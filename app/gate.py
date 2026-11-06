"""The decision gate: READY or ABSTAIN, by plain rules. It lists actions but never chooses one."""
import json

# Supporting a new discrepancy type means adding one line here and nothing else.
REQUIRED_FACTS = {
    "DOSE_CONFLICT": ["effective_date_a", "effective_date_b", "change_reason"],
    "FREQUENCY_CONFLICT": ["effective_date_a", "effective_date_b", "change_reason"],
    "ROUTE_CONFLICT": ["effective_date_a", "effective_date_b", "change_reason"],
    "MISSING": ["stop_reason", "current_intake"],
    "DUPLICATE": ["same_or_separate"],
    "UNREADABLE": ["legible_source"],
}
MIN_CONFIDENCE = 0.80
ENTRY_FIELDS = ("effective_date", "dose", "unit", "frequency_per_day")
SAFE_ACTIONS = ["request_fact", "escalate", "keep_pending"]
CANDIDATE_ACTIONS = [
    ("accept_a", "Accept the earlier record (A)"),
    ("accept_b", "Accept the later record (B)"),
    ("both_valid", "Both records are valid"),
]
MISSING_CANDIDATE_ACTIONS = [("accept_a", "Keep on list"), ("accept_b", "Confirm removed")]
FREQUENCY_WORDS = {1: "once daily", 2: "twice daily", 3: "thrice daily"}


def load_entry(conn, entry_id):
    if entry_id is None:
        return None
    row = conn.execute(
        "SELECT e.*, d.facility, d.doc_type, d.doc_date, d.prescriber"
        " FROM med_entries e JOIN documents d ON d.id = e.document_id WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    return dict(row) if row else None


def supplied_by_records(name, entries):
    if name == "change_reason":
        return bool(entries["b"] and entries["b"]["note"])
    field, _, side = name.rpartition("_")
    entry = entries.get(side)
    return bool(entry) and entry.get(field) is not None


def find_missing(conn, discrepancy, entries):
    # An unknown type fails closed: nobody has said which facts it needs.
    wanted = list(REQUIRED_FACTS.get(discrepancy["type"], ["required_facts_for_this_type"]))
    for side, entry in entries.items():
        if entry:
            wanted += [f"{field}_{side}" for field in ENTRY_FIELDS]
            if entry["confidence"] < MIN_CONFIDENCE:
                wanted.append(f"entry_{side}_confirmed")
    added = {row["name"] for row in conn.execute("SELECT name FROM facts WHERE discrepancy_id = ?", (discrepancy["id"],))}
    missing = []
    for name in wanted:
        if name not in missing and name not in added and not supplied_by_records(name, entries):
            missing.append(name)
    return missing


def describe_value(kind, entry):
    if kind == "DOSE_CONFLICT" and entry["dose"] is not None and entry["unit"]:
        return f"{entry['dose']} {entry['unit']}"
    if kind == "FREQUENCY_CONFLICT" and entry["frequency_per_day"] is not None:
        return FREQUENCY_WORDS.get(entry["frequency_per_day"], f"{entry['frequency_per_day']} times a day")
    if kind == "ROUTE_CONFLICT" and entry["route"]:
        return entry["route"]
    return "an unreadable value"


def describe_fact(name, entries):
    labels = {
        "change_reason": "reason for the change",
        "stop_reason": "why it is absent from the later record",
        "current_intake": "whether the patient is still taking it",
    }
    if name in labels:
        return labels[name]
    if name.startswith("entry_") and name.endswith("_confirmed"):
        return f"confirmation of the {entries[name[6]]['doc_type'] or 'unnamed'} record against its source"
    field, _, side = name.rpartition("_")
    if entries.get(side):
        return f"{field.replace('_', ' ')} of the {entries[side]['doc_type'] or 'unnamed'} record"
    return name.replace("_", " ")


def build_question(discrepancy, entries, missing):
    labels = [describe_fact(name, entries) for name in missing]
    needed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    kind = discrepancy["type"]
    if kind in ("DOSE_CONFLICT", "FREQUENCY_CONFLICT", "ROUTE_CONFLICT"):
        ask = f"Ask the prescriber whether {describe_value(kind, entries['b'])} replaced {describe_value(kind, entries['a'])}."
    elif kind == "MISSING":
        ask = "Ask the prescriber and the patient."
    else:
        ask = "Ask the prescriber."
    return f"Needed for {discrepancy['drug_norm']}: {needed}. {ask}"


def evaluate(conn, discrepancy_id):
    """Return {decision, missing, candidate_actions, safe_actions, question}. Nothing is ever selected."""
    discrepancy = conn.execute("SELECT * FROM discrepancies WHERE id = ?", (discrepancy_id,)).fetchone()
    entries = {"a": load_entry(conn, discrepancy["entry_a_id"]), "b": load_entry(conn, discrepancy["entry_b_id"])}
    missing = find_missing(conn, discrepancy, entries)
    candidates = MISSING_CANDIDATE_ACTIONS if discrepancy["type"] == "MISSING" else CANDIDATE_ACTIONS
    ready = not missing
    return {
        "decision": "READY" if ready else "ABSTAIN",
        "missing": missing,
        "candidate_actions": [{"action": action, "label": label, "selected": False} for action, label in candidates],
        "safe_actions": ([action for action, _ in candidates] if ready else []) + SAFE_ACTIONS,
        "question": None if ready else build_question(discrepancy, entries, missing),
    }


def store_decision(conn, discrepancy_id):
    """Evaluate and save the result on the discrepancy, inside the caller's open transaction."""
    result = evaluate(conn, discrepancy_id)
    conn.execute(
        "UPDATE discrepancies SET gate_decision = ?, missing_facts = ? WHERE id = ?",
        (result["decision"], json.dumps(result["missing"]), discrepancy_id),
    )
    return result

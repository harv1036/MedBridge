# MedBridge

PS4 "The Forgotten Prescription": medication continuity and safe handover. MedBridge compares the medicine lists in a patient's documents, flags differences with plain rules, and refuses to let anyone decide a difference while a decision-critical fact is missing. No LLM is used anywhere.

## Run it

```bash
pip install -r requirements.txt
python seed.py
uvicorn app.main:app
```

Open http://127.0.0.1:8000. `bash run_demo.sh` does the last two steps in one go (set `PORT=8765` to use another port). Run the tests with `pytest -q`; they start their own server on a temporary database and never touch the demo data.

Only one server should use `data/medbridge.db` at a time. Two servers on the same file work, but the demo then changes under you.

## The four seeded patients

| Patient | Documents | What it shows |
|---|---|---|
| 1 Ravi | Metformin 500 mg (clinic), then 1000 mg with no date (discharge) | Dose conflict. The gate says ABSTAIN until the discharge date and the reason for the change are added with sources. |
| 2 Meera | Amlodipine on the clinic list, absent from the discharge | Flagged `MISSING`, never "stopped". ABSTAIN until `stop_reason` and `current_intake` are known. |
| 3 Arjun | Glycomet 500 mg, then Metformin 500 mg; atorvastatin 10 mg, then 20 mg "increased after LDL review" | Glycomet and Metformin match by generic, so no flag. The atorvastatin change is an intentional change and the gate says READY. |
| 4 Sunita | Telmisartan 40 mg, then 80 mg "dose increased per cardiology review" | READY. Also the item used for the simultaneous-claim and duplicate-submission tests. |

## Demo walkthrough

1. Open the app, set **Acting as** to `pharmacist_a`, open Review Queue.
2. Claim patient 1's metformin item. The red ABSTAIN box names what is missing; the accept buttons are greyed out and marked unselected.
3. Add `effective_date_b` (2026-04-20) with a source, then `change_reason` with a source. The gate turns READY.
4. Accept B. Open Audit, press Verify chain, then open Patient View for patient 1.
5. Concurrency Lab: run N simultaneous claims, then N identical submissions.

## Tests

| Test | What it proves |
|---|---|
| T1 | 20 threads claim the same item at the same instant: exactly one 200 and nineteen 409. The audit log holds 1 `claim` and 19 `claim_rejected`. Also run across two server processes. |
| T2 | The same resolve with the same Idempotency-Key sent 10 times gives one `resolutions` row and ten identical responses. |
| T3 | The same resolve with no key sent 5 times gives one row, because a key is derived from actor, endpoint, item and body. |
| T4 | The same document uploaded 5 times in parallel gives one document row and no extra discrepancy. |
| T5 | Resolving with an old `expected_version` gives 409 `STALE_VERSION` and changes nothing. |
| T6 | Patient 1's dose conflict is ABSTAIN with `effective_date_b` missing, and `accept_b` returns 422 `BLOCKED_MISSING_FACT`. |
| T7 | After the facts are added with sources, the gate is READY, `accept_b` succeeds, and the audit log holds `fact_added` with source and actor. |
| T8 | The audit chain verifies; after one row is edited directly in SQLite, verify names that row. |
| T9 | Patient 1's patient view is Unresolved and its response contains no dose. |

`tests/test_compare.py` covers extraction, name matching and the comparison rules. `tests/test_gate.py` covers the gate and the patient view.

## Safety rules, and where they are enforced

- Nothing generates prescriptions, substitutes medicines, recommends dose changes or tells a patient to stop. The patient view only repeats what the documents say.
- Source text is never edited. A fact is a new row; `med_entries` rows are never updated.
- A medicine missing from a later document is flagged `MISSING`, never assumed stopped.
- The newest record is never assumed correct. While an item is Pending, the patient view shows every record.
- The gate (`app/gate.py`) lists candidate actions with `selected: false` and never fills a missing field.

## Decisions worth knowing

- A blank effective date or note does not lower extraction confidence. The gate treats a null effective date as a missing fact instead.
- Patient 1 needs both `effective_date_b` and `change_reason`, because `REQUIRED_FACTS` requires both and the discharge note is blank.
- Writers in one server process queue on a lock before `BEGIN IMMEDIATE`. Without it, sustained load produced `database is locked` after the 5 second wait. SQLite still protects against other processes.
- Extra error codes beyond the brief: `NOT_FOUND`, `INVALID_DOCUMENT`, `INVALID_REQUEST`, `MISSING_ACTOR`. Extra endpoint: `GET /audit`, for the Audit tab.

## Data

`data/drug_master.csv` has 696 rows and 12 generics. It was built from the public [Indian Medicine Dataset](https://github.com/junioralive/Indian-Medicine-Dataset) (MIT licence, commit `7c06a88`): single-ingredient, current tablets and capsules with a stated strength, at most 60 products per generic. The patient documents in `data/docs/` are synthetic.

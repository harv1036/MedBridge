"""HTTP endpoints. Handlers are plain def, so each request runs in its own thread."""
from contextlib import closing

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.compare import compare_report
from app.db import connect, transaction
from app.extract import ingest_document, parse_document

router = APIRouter()


def error(status_code, code, message, current_state=None):
    return JSONResponse(
        status_code=status_code, content={"code": code, "message": message, "current_state": current_state}
    )


def patient_exists(conn, patient_id):
    return conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is not None


async def read_body(request: Request):
    """Async only to read the bytes. Any content type works, so plain text is never parsed as JSON."""
    return await request.body()


@router.post("/patients/{patient_id}/documents")
def upload_document(patient_id: int, body: bytes = Depends(read_body)):
    try:
        raw_text = body.decode("utf-8")
    except UnicodeDecodeError:
        return error(422, "INVALID_DOCUMENT", "The document must be UTF-8 text.")
    _, entries = parse_document(raw_text)
    if not entries:
        return error(422, "INVALID_DOCUMENT", "No MED lines found, so nothing was stored.")
    with transaction() as conn:
        if not patient_exists(conn, patient_id):
            return error(404, "NOT_FOUND", f"Patient {patient_id} does not exist.")
        result = ingest_document(conn, patient_id, raw_text)
    return JSONResponse(status_code=200 if result["duplicate"] else 201, content=result)


@router.get("/patients/{patient_id}/compare")
def get_compare(patient_id: int):
    with closing(connect()) as conn:
        if not patient_exists(conn, patient_id):
            return error(404, "NOT_FOUND", f"Patient {patient_id} does not exist.")
        return compare_report(conn, patient_id)

"""MedBridge application. Run with: uvicorn app.main:app"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.routes import router

INDEX_PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"

app = FastAPI(title="MedBridge")
app.include_router(router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_PAGE)

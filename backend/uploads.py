from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "data/uploads"))


async def save_uploads(files: list[UploadFile]) -> list[dict[str, str]]:
    """Save validated source files outside static web roots with generated filenames."""
    if not files:
        return []
    batch = UPLOAD_ROOT / str(uuid.uuid4())
    batch.mkdir(parents=True, exist_ok=False)
    saved: list[dict[str, str]] = []
    for file in files:
        original_name = Path(file.filename or "upload").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(415, "Only CSV, XLSX, and XLSM source files are accepted")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Each upload must be between 1 byte and {MAX_UPLOAD_BYTES} bytes")
        path = batch / f"{uuid.uuid4()}{suffix}"
        path.write_bytes(content)
        saved.append({"name": original_name, "path": str(path), "size_bytes": str(len(content))})
    return saved

"""Bounded multipart parsing after authentication/security/role checks."""

import hashlib

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from 主程序.repositories.xlsx_reader import InputError

# Existing server raw CSV is 15,345,181 bytes; 20 MiB leaves explicit headroom,
# not enough for the 45 MB source XLSX. Envelope overhead is separately bounded.
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024


class BoundedParser(MultiPartParser):
    # Keep bounded uploads in memory, never spill sensitive CSV to OS temp.
    max_file_size = MAX_BODY_BYTES
    max_part_size = 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ended = False

    def on_end(self):
        self.ended = True


async def multipart(request, model, validate):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise InputError("VALIDATION_ERROR", 422, fields=["body"])

    async def bounded():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise InputError("UPLOAD_TOO_LARGE", 413, max_file_bytes=MAX_FILE_BYTES)
            # Do not let one ASGI chunk build oversized parser callback queues.
            for start in range(0, len(chunk), 65536):
                yield chunk[start:start + 65536]

    parser = BoundedParser(request.headers, bounded(), max_files=1, max_fields=len(model.model_fields))
    form = None
    try:
        form = await parser.parse()
        pairs = form.multi_items()
        if not parser.ended or len(pairs) != len({k for k, _ in pairs}):
            raise InputError("VALIDATION_ERROR", 422, fields=["form"])
        values = dict(pairs)
        file = values.pop("file", None)
        if not isinstance(file, UploadFile) or any(not isinstance(v, str) for v in values.values()):
            raise InputError("VALIDATION_ERROR", 422, fields=["file"])
        data = validate(model, values)
        mime = (file.content_type or "").split(";", 1)[0].strip().lower()
        if not file.filename or not file.filename.lower().endswith(".csv") or mime not in {"", "text/csv", "application/csv", "application/octet-stream", "application/vnd.ms-excel", "text/plain"}:
            raise InputError("VALIDATION_ERROR", 422, fields=["file"])
        digest, size = hashlib.sha256(), 0
        while chunk := await file.read(65536):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise InputError("UPLOAD_TOO_LARGE", 413, max_file_bytes=MAX_FILE_BYTES)
            digest.update(chunk)
        if not size:
            raise InputError("CORRUPT_FILE", 400)
        return data, digest.hexdigest()
    except InputError:
        raise
    except Exception as exc:
        # Parser text may contain submitted headers or filenames; never echo it.
        raise InputError("VALIDATION_ERROR", 422, fields=["form"]) from exc
    finally:
        if form is not None:
            await form.close()
        # Starlette closes only MultiPartException paths; also cover streamed
        # limits/disconnects/malformed-parser exceptions and incomplete parts.
        for file in parser._files_to_close_on_error:
            file.close()

import base64
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from backend.app.data_dir import data_dir

INLINE_TEXT_PREFIX = "inline-text:"
LIBRARY_FILE_ROOTS_ENV = "DTXWIKI_LIBRARY_FILE_ROOTS"


class LibraryStorageError(ValueError):
    pass


def inline_text_ref(raw_text: str) -> str:
    encoded = base64.urlsafe_b64encode(raw_text.encode("utf-8")).decode("ascii")
    return f"{INLINE_TEXT_PREFIX}{encoded}"


def read_text_ref(storage_ref: str) -> str:
    if storage_ref.startswith(INLINE_TEXT_PREFIX):
        payload = storage_ref.removeprefix(INLINE_TEXT_PREFIX)
        return base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    if storage_ref.startswith("file://"):
        parsed = urlparse(storage_ref)
        path = Path(url2pathname(parsed.path)).expanduser().resolve()
        if not _is_allowed_file_ref(path):
            raise LibraryStorageError(
                f"file source ref is outside allowed roots: {storage_ref}"
            )
        return path.read_text(encoding="utf-8")
    raise LibraryStorageError(f"unsupported source storage ref: {storage_ref}")


def allowed_file_roots() -> list[Path]:
    configured = os.environ.get(LIBRARY_FILE_ROOTS_ENV)
    if configured:
        return [
            Path(part).expanduser().resolve()
            for part in configured.split(os.pathsep)
            if part
        ]
    return [(data_dir() / "library").resolve()]


def _is_allowed_file_ref(path: Path) -> bool:
    for root in allowed_file_roots():
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False

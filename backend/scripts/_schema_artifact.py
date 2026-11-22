"""Publish generated schema files without exposing incomplete JSON."""

from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory


def write_schema_artifact(path: Path, content: str) -> None:
    # Stage on the destination filesystem so replacement is atomic.
    with TemporaryDirectory(prefix=f".{path.name}.", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        staged.write_text(content, encoding="utf-8")
        try:
            previous_mode = S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            staged.chmod(previous_mode)
        staged.replace(path)

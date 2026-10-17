"""Resource identity and strict lifetime guards shared by runtime participants.

Owner locks serialize API launchers. Activity locks survive in each child and
block a replacement or maintenance operation even after its parent has died.
The worker owns the consumer lock itself, including in external deployments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.util import asbool

from backend.app.data_dir import data_dir
from backend.services.ops import runtime_lock

ACTIVITY_ENV = "_DTXWIKI_RUNTIME_ACTIVITY"


class RuntimeResourceError(ValueError):
    """The requested storage cannot provide the managed ownership contract."""


class LockSet:
    def __init__(self, locks=()) -> None:
        self._locks = list(locks)

    def add(self, lock) -> None:
        self._locks.append(lock)

    def release(self) -> None:
        while self._locks:
            self._locks.pop().release()

    @property
    def held(self) -> bool:
        return bool(self._locks) and all(lock.held for lock in self._locks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def acquire_paths(paths, mode: str) -> LockSet:
    guards = LockSet()
    try:
        for path in sorted(set(map(Path, paths)), key=str):
            guards.add(runtime_lock.acquire_path(path, mode, strict=True))
    except BaseException:
        guards.release()
        raise
    return guards


@dataclass(frozen=True)
class RuntimeResources:
    root: Path
    database: Path | None

    @classmethod
    def for_settings(cls, settings, *, managed: bool = False):
        url = make_url(settings.database_url)
        database = None
        if url.get_backend_name() == "sqlite":
            name = url.database
            try:
                uri = asbool(url.query.get("uri", False))
            except (ValueError, TypeError) as exc:
                raise RuntimeResourceError("invalid SQLite URI option") from exc
            if name and name != ":memory:" and not name.startswith("file:"):
                if not uri:
                    # SQLAlchemy's SQLite dialect treats '~' literally; shell
                    # home expansion here would lock a different physical DB.
                    database = Path(name).resolve()
            if managed and (
                database is None or url.drivername not in {"sqlite", "sqlite+pysqlite"}
            ):
                raise RuntimeResourceError(
                    "managed runtime requires a regular-file SQLite database; "
                    "memory and SQLite URI databases require external mode"
                )
        elif managed:
            raise RuntimeResourceError(
                "managed runtime supports local file SQLite only; use external mode"
            )
        if database is not None and database.exists():
            if not database.is_file() or database.stat().st_nlink > 1:
                raise RuntimeResourceError(
                    "runtime SQLite database must be a regular file without hard links"
                )
        return cls(data_dir().resolve(), database)

    @property
    def owner_paths(self) -> tuple[Path, ...]:
        paths = [self.root / ".runtime-owner.lock"]
        if self.database is not None:
            paths.append(
                self.database.with_name(self.database.name + ".dtxwiki-owner.lock")
            )
        return tuple(paths)

    @property
    def activity_paths(self) -> tuple[Path, ...]:
        paths = [self.root / runtime_lock.LOCK_FILENAME]
        if self.database is not None:
            paths.append(
                self.database.with_name(self.database.name + ".dtxwiki-activity.lock")
            )
        return tuple(paths)

    def acquire_owner(self) -> LockSet:
        return acquire_paths(self.owner_paths, "exclusive")

    def acquire_activity(self, mode: str = "shared") -> LockSet:
        return acquire_paths(self.activity_paths, mode)

    def acquire_consumer(self) -> LockSet:
        paths = (
            [self.database.with_name(self.database.name + ".dtxwiki-consumer.lock")]
            if self.database is not None
            else []
        )
        return acquire_paths(paths, "exclusive")

    def acquire_parent(self, *, managed: bool) -> LockSet:
        guards = self.acquire_owner()
        try:
            if managed:
                # No work occurs during the non-atomic downgrade. Children must
                # register their independent SH before receiving start authority.
                self.acquire_activity("exclusive").release()
            guards.add(self.acquire_activity("shared"))
        except BaseException:
            guards.release()
            raise
        return guards

    def export_environment(self) -> dict[str, str]:
        return {ACTIVITY_ENV: json.dumps([str(p) for p in self.activity_paths])}

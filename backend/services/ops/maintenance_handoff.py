"""Verify updater-owned locks across console launchers without PID ancestry.

The listener exists only while one finalizer runs. Its private environment
token authorizes a challenge against the parent's actual, still-held guards.
No descriptor inheritance or direct parent/interpreter relationship is needed.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

HANDOFF_ENV = "_DTXWIKI_MAINTENANCE_LOCK_HANDOFF"
_PROTOCOL = 1
_PACKET_LIMIT = 65_536
_IO_TIMEOUT = 2.0


class HandoffError(RuntimeError):
    """The finalizer could not verify live ownership of its storage guards."""


def _paths(paths) -> tuple[str, ...]:
    result = tuple(sorted({os.path.normcase(str(Path(p).resolve())) for p in paths}))
    if not result:
        raise ValueError("empty resource identity")
    return result


def _send(connection: socket.socket, value: dict) -> None:
    packet = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    if len(packet) > _PACKET_LIMIT:
        raise ValueError("oversized handoff packet")
    connection.sendall(packet)


def _receive(connection: socket.socket) -> dict:
    deadline = time.monotonic() + _IO_TIMEOUT
    packet = bytearray()
    while b"\n" not in packet:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("handoff response deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, _PACKET_LIMIT + 1 - len(packet)))
        if not chunk:
            raise ValueError("incomplete handoff packet")
        packet.extend(chunk)
        if len(packet) > _PACKET_LIMIT:
            raise ValueError("oversized handoff packet")
    value = json.loads(packet)
    if not isinstance(value, dict):
        raise ValueError("invalid handoff packet")
    return value


@contextmanager
def finalizer_handoff(held: object):
    """Yield private metadata while retaining a live challenge for ``held``.

    The caller keeps the guards acquired until this context and its finalizer
    subprocess have both completed. This verifies entry, rather than promising
    supervision if the updater itself dies during a migration.
    """
    try:
        expected = _paths(held.paths)
        if not held.held:
            raise ValueError("guards are not held")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise HandoffError("maintenance lock handoff requires held guards") from exc

    token = secrets.token_hex(32)
    stopped = threading.Event()
    failed = threading.Event()
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise HandoffError("maintenance lock handoff listener could not start") from exc
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.1)
    except OSError as exc:
        listener.close()
        raise HandoffError("maintenance lock handoff listener could not start") from exc

    def serve() -> None:
        while not stopped.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not stopped.is_set():
                    failed.set()
                return
            with connection:
                connection.settimeout(_IO_TIMEOUT)
                try:
                    request = _receive(connection)
                    supplied = request.get("token")
                    accepted = (
                        set(request) == {"protocol", "token", "paths"}
                        and type(request["protocol"]) is int
                        and request["protocol"] == _PROTOCOL
                        and isinstance(supplied, str)
                        and secrets.compare_digest(supplied, token)
                        and request["paths"] == list(expected)
                        and held.held
                        and _paths(held.paths) == expected
                    )
                    _send(connection, {"ok": bool(accepted)})
                except (OSError, TypeError, ValueError, RecursionError):
                    # Invalid/disconnected callers never gain authority and
                    # cannot consume the legitimate finalizer's challenge.
                    continue
                except Exception:
                    failed.set()
                    return

    thread = threading.Thread(
        target=serve, name="dtxwiki-maintenance-handoff", daemon=True
    )
    try:
        thread.start()
    except RuntimeError as exc:
        listener.close()
        raise HandoffError("maintenance lock handoff listener could not start") from exc
    completed = False
    try:
        yield json.dumps(
            {"protocol": _PROTOCOL, "port": listener.getsockname()[1], "token": token},
            separators=(",", ":"),
        )
        completed = True
    finally:
        stopped.set()
        listener.close()
        thread.join(_IO_TIMEOUT + 1.0)
        if completed and (failed.is_set() or thread.is_alive()):
            raise HandoffError("maintenance lock handoff listener failed")


def verify_finalizer_handoff(paths) -> bool:
    """Verify and consume a new updater's marker; return False for old callers.

    Present but invalid metadata fails closed. A stale handoff must never be
    interpreted as an old updater, nor grant permission to skip storage guards.
    """
    raw = os.environ.pop(HANDOFF_ENV, None)
    if raw is None:
        return False
    try:
        if len(raw.encode()) > _PACKET_LIMIT:
            raise ValueError("oversized metadata")
        metadata = json.loads(raw)
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"protocol", "port", "token"}
            or type(metadata["protocol"]) is not int
            or metadata["protocol"] != _PROTOCOL
            or type(metadata["port"]) is not int
            or not 0 < metadata["port"] < 65536
            or not isinstance(metadata["token"], str)
            or len(metadata["token"]) != 64
        ):
            raise ValueError("invalid metadata")
        with socket.create_connection(
            ("127.0.0.1", metadata["port"]), timeout=_IO_TIMEOUT
        ) as connection:
            _send(
                connection,
                {
                    "protocol": _PROTOCOL,
                    "token": metadata["token"],
                    "paths": list(_paths(paths)),
                },
            )
            response = _receive(connection)
            if set(response) != {"ok"} or response["ok"] is not True:
                raise ValueError("handoff refused")
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise HandoffError("maintenance lock handoff could not be verified") from exc
    return True

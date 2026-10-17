"""Small private child channel. Install parent-loss handling before app imports."""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading

PROTOCOL = 2
PACKET_LIMIT = 1_048_576
PARENT_LOST_GRACE_SECONDS = 10.0


class ChildShutdownRequested(BaseException):
    pass


class ChildControl:
    def __init__(self, role: str) -> None:
        from backend.app.process_tree import join_runtime_job

        join_runtime_job()
        self.role = role
        self.stop_requested = threading.Event()
        self._messages = queue.Queue(maxsize=4)
        self._interrupt = None
        self._timer = None
        self._output = sys.stdout
        self._output_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._interrupt_sent = False
        threading.Thread(
            target=self._read, name="dtxwiki-parent-watch", daemon=True
        ).start()

    def _stop(self) -> None:
        with self._state_lock:
            if self.stop_requested.is_set():
                return
            self.stop_requested.set()
            self._timer = threading.Timer(PARENT_LOST_GRACE_SECONDS, self._kill)
            self._timer.daemon = True
            self._timer.start()
            if self._interrupt is not None:
                self._interrupt_sent = True
                self._interrupt()

    @staticmethod
    def _kill() -> None:
        if os.name != "nt" and os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(1)

    def _read(self) -> None:
        try:
            while True:
                line = sys.stdin.readline(PACKET_LIMIT + 1)
                if not line or line.strip() == "shutdown":
                    break
                if len(line.encode("utf-8")) > PACKET_LIMIT or not line.endswith("\n"):
                    break
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                if value.get("command") == "shutdown":
                    break
                self._messages.put_nowait(value)
        except (OSError, ValueError, RecursionError, queue.Full):
            pass
        self._stop()

    def set_interrupt(self, callback) -> None:
        with self._state_lock:
            self._interrupt = callback
            if self.stop_requested.is_set() and not self._interrupt_sent:
                self._interrupt_sent = True
                callback()

    def _receive(self):
        while not self.stop_requested.is_set():
            try:
                return self._messages.get(timeout=0.05)
            except queue.Empty:
                continue
        raise ChildShutdownRequested()

    def read_config(self) -> dict:
        packet = self._receive()
        if (
            packet.get("protocol") != PROTOCOL
            or packet.get("role") != self.role
            or not isinstance(packet.get("settings"), dict)
        ):
            raise ValueError("invalid private runtime configuration")
        return packet

    def registered(self) -> None:
        self.emit({"status": "registered", "role": self.role})

    def wait_for_start(self) -> None:
        if self._receive() != {"command": "start"}:
            raise ValueError("invalid runtime start command")

    def emit(self, packet: dict) -> None:
        if self.stop_requested.is_set():
            raise ChildShutdownRequested()
        with self._output_lock:
            self._output.write(json.dumps(packet) + "\n")
            self._output.flush()

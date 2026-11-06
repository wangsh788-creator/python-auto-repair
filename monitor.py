import os
from pathlib import Path
import time
import threading
import uuid

from core import inventory, read_state


POLL_SECONDS = 2
RETRY_SECONDS = 60


def folder_snapshot(state_dir):
    state = read_state(state_dir)
    if not state:
        return None
    folder = Path(state["folder"])
    if not folder.is_dir():
        return None
    exported = {os.path.normcase(str(Path(name).resolve())) for name in state.get("exports", [])}
    entries = []
    for name, path in inventory(folder).items():
        if os.path.normcase(str(path.resolve())) in exported:
            continue
        try:
            details = path.stat()
            entries.append((name, details.st_size, details.st_mtime_ns))
        except OSError:
            entries.append((name, None, None))
    return str(folder.resolve()), tuple(entries), tuple(state.get("pending_open", []))


class FolderMonitor:
    def __init__(self, state_dir, *, clock=time.monotonic, snapshot=folder_snapshot,
                 poll_seconds=POLL_SECONDS, retry_seconds=RETRY_SECONDS):
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.snapshot = snapshot
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self.last_snapshot = None
        self.active_snapshot = None
        self.active = False
        self.retry = False
        self.next_check = 0
        self.lock = threading.Lock()

    def due(self):
        with self.lock:
            return not self.active and self.clock() >= self.next_check

    def begin(self, automatic=True):
        with self.lock:
            if self.active or (automatic and self.clock() < self.next_check):
                return False
            self.active = True
        try:
            current = self.snapshot(self.state_dir)
        except Exception:
            with self.lock:
                self.active = False
            raise
        with self.lock:
            self.next_check = self.clock() + self.poll_seconds
            if current is None or (automatic and current == self.last_snapshot and not self.retry):
                self.active = False
                return False
            self.active_snapshot = current
            return True

    def finish(self, failed=False):
        with self.lock:
            if self.active:
                self.last_snapshot = self.active_snapshot
            self.active = False
            self.active_snapshot = None
            self.retry = bool(failed)
            self.next_check = self.clock() + (self.retry_seconds if failed else self.poll_seconds)

    def invalidate(self):
        with self.lock:
            self.last_snapshot = None
            self.retry = True
            self.next_check = 0


def request_settings(state_dir):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ("settings-request-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(b"show\n")
        os.replace(temporary, directory / "settings.request")
    finally:
        temporary.unlink(missing_ok=True)


def consume_settings_request(state_dir):
    path = Path(state_dir) / "settings.request"
    try:
        with path.open("rb") as stream:
            command = stream.read(32)
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return command == b"show\n"

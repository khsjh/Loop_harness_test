"""Immutable, fsynced events; state.json is a replaceable derived cache."""
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import uuid

from .contracts import ContractError, loads
from .process_runner import now
from .recording import json_bytes, write_once


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, *parts):
    root = Path(root).resolve()
    if any(Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
        raise ContractError("unsafe controller path")
    path = root.joinpath(*parts)
    if not path.is_relative_to(root):
        raise ContractError("path outside controller root")
    for parent in (*path.relative_to(root).parents,):
        if (root / parent).is_symlink():
            raise ContractError("symlink in controller path")
    if path.is_symlink():
        raise ContractError("symlink in controller path")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def commit(path, value, *, raw=False):
    write_once(path, value if raw else json_bytes(value))
    sync_directory(path.parent)


def replace_json(path, value):
    if path.is_symlink():
        raise ContractError("symlink cache/guard")
    temporary = path.parent / (".state-" + uuid.uuid4().hex)
    try:
        commit(temporary, value)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def run_path(root, run_id):
    if type(run_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id):
        raise ContractError("invalid run_id")
    return safe_path(root, "runs", run_id)


class RunStore:
    def __init__(self, root, request=None, *, run_id=None):
        self.root = Path(root).resolve()
        self.path = run_path(root, request["run_id"] if request else run_id)
        self.head = None
        self.state = None
        self.lease = None
        if request is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.mkdir(mode=0o700)
            self.lease = (self.path / ".controller.lock").open("a+")
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sync_directory(self.path.parent)
            for directory in ("events", "snapshots", "rounds", "final", "recovery"):
                (self.path / directory).mkdir()
            commit(self.path / "run-request.json", request)
            self.event("accepted", {
                "schema_version": 1, "run_id": request["run_id"], "phase": "accepted", "status": None,
                "reason_code": None, "last_valid_phase": "accepted", "rounds_started": 0,
                "max_rounds": request["limits"]["max_rounds"], "active_operation": None,
                "code_state_id": None, "task_outcome": None, "recording_status": "pending",
                "dialog_owner": request.get("dialog_owner", "harness"),
                "recovery_required": False, "started_at": now(), "ended_at": None,
                "dialog_call_id": (request["caller_dialog"]["call_id"] if request.get("caller_dialog")
                                   else "dialog-" + uuid.uuid4().hex),
            }, initial=True)
        else:
            self.reload()

    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None

    @contextmanager
    def writer(self):
        if self.lease is not None:
            raise ContractError("run already has a writer")
        path = safe_path(self.path, ".controller.lock")
        self.lease = path.open("a+")
        try:
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ContractError("run controller is active") from exc
            self.reload()
            yield self
        finally:
            self.close()

    def reload(self):
        files = sorted(safe_path(self.path, "events").glob("*.json"))
        self.events = []
        if not files:
            raise ContractError("missing M5 events")
        previous = None
        for number, file in enumerate(files, 1):
            if file.name != f"{number:06}.json" or file.is_symlink():
                raise ContractError("event sequence gap or symlink")
            raw = file.read_bytes()
            event = loads(raw)
            if event.get("content_hash") != digest(json_bytes({k: v for k, v in event.items() if k != "content_hash"})):
                raise ContractError("event content hash mismatch")
            if event["sequence"] != number or event["previous_hash"] != previous:
                raise ContractError("event chain mismatch")
            if event["state"]["run_id"] != self.path.name or event["state"]["sequence"] != number:
                raise ContractError("event state identity mismatch")
            for name, expected in event["artifacts"].items():
                path = safe_path(self.path, name)
                if digest(path.read_bytes()) != expected:
                    raise ContractError("artifact hash mismatch: " + name)
            previous = digest(raw)
            self.state = event["state"]
            self.events.append(event)
        self.head = previous
        return self.state

    def event(self, name, changes=None, *, artifacts=(), initial=False):
        if self.lease is None:
            raise ContractError("event mutation requires controller lease")
        if not initial:
            self.reload()
        state = copy.deepcopy(self.state) if self.state else {}
        old_phase = state.get("phase")
        if state.get("status") is not None:
            if name not in {"recording_repaired", "recovery_confirmed", "caller_dialog_recorded"} or set(changes or {}) - {"recording_status", "recovery_required"}:
                raise ContractError("terminal execution cannot resume")
        state.update(changes or {})
        state["sequence"] = (self.state["sequence"] if self.state else 0) + 1
        event = {"sequence": state["sequence"], "previous_hash": self.head,
                 "event": name, "at": now(), "from_phase": old_phase, "state": state,
                 "artifacts": {str(Path(p).relative_to(self.path)): digest(Path(p).read_bytes()) for p in artifacts}}
        event["content_hash"] = digest(json_bytes(event))
        path = safe_path(self.path, "events", f"{state['sequence']:06}.json")
        commit(path, event)
        self.head, self.state = digest(path.read_bytes()), state
        replace_json(self.path / "state.json", state)
        return state

    def finish(self, status, reason, **extra):
        if status not in {"success", "failed", "limit_reached", "waiting_for_user"}:
            raise ContractError("invalid terminal status")
        state = self.event("terminated", {"phase": "terminal", "status": status, "reason_code": reason,
                                          "ended_at": now(), **extra})
        commit(self.path / "terminal.json", state)
        return state

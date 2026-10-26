"""Nonblocking workspace leases and durable guards against uncertain re-entry."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .contracts import ContractError, loads
from .process_runner import group_alive
from .run_store import digest, replace_json, safe_path


class WorkspaceBusy(ContractError):
    pass


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": fields[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def identity_alive(identity):
    return identity is not None and process_identity(identity["pid"]) == identity


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


@contextmanager
def global_lock(root):
    directory = safe_path(root, "runs", ".locks")
    directory.mkdir(parents=True, exist_ok=True)
    path = safe_path(root, "runs", ".locks", "registry.lock")
    with path.open("a+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


class WorkspaceGuard:
    def __init__(self, root, workspace, run_id):
        self.root, self.workspace, self.run_id = Path(root).resolve(), Path(workspace).resolve(), run_id
        stat = self.workspace.stat()
        self.key = digest(f"{stat.st_dev}:{stat.st_ino}".encode())
        self.file = None
        self.path = safe_path(root, "runs", ".guards", self.key + ".json")
        self.data = None

    def __enter__(self):
        with global_lock(self.root):
            self.path.parent.mkdir(exist_ok=True)
            for path in sorted(self.path.parent.glob("*.json")):
                if path.is_symlink():
                    raise ContractError("symlink workspace guard")
                data = loads(path.read_bytes())
                if not (overlaps(self.workspace, Path(data["workspace"])) or path == self.path):
                    continue
                lock_path = safe_path(self.root, "runs", ".locks", path.stem + ".lock")
                with lock_path.open("a+") as candidate:
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise WorkspaceBusy("workspace is leased by " + data["run_id"]) from exc
                if data["active"]:
                    raise WorkspaceBusy("workspace needs recovery: " + data["run_id"])
            lock_path = safe_path(self.root, "runs", ".locks", self.key + ".lock")
            self.file = lock_path.open("a+")
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.data = {"workspace": str(self.workspace), "run_id": self.run_id,
                             "controller": process_identity(os.getpid()), "active": False, "process": None}
                replace_json(self.path, self.data)
            except BaseException:
                self.file.close()
                self.file = None
                raise
        return self

    def planned(self):
        self.data.update(active=True, process=None)
        replace_json(self.path, self.data)

    def started(self, process):
        identity = process_identity(process.pid)
        if identity is None:
            raise OSError("cannot identify spawned process for recovery")
        self.data.update(process=identity)
        replace_json(self.path, self.data)

    def cleared(self):
        self.data.update(active=False, process=None)
        replace_json(self.path, self.data)

    def __exit__(self, *exc):
        if self.file:
            self.file.close()
            self.file = None


def recover_guard(root, workspace, run_id):
    """Clear only an owned, verifiably stopped group. Never kill a reused PID."""
    guard = WorkspaceGuard(root, workspace, run_id)
    with global_lock(root):
        if not guard.path.exists():
            return True
        data = loads(guard.path.read_bytes())
        if data["run_id"] != run_id:
            raise WorkspaceBusy("workspace now belongs to another execution")
        lock_path = safe_path(root, "runs", ".locks", guard.key + ".lock")
        with lock_path.open("a+") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceBusy("controller still owns workspace") from exc
            if data["active"]:
                identity = data["process"]
                if (identity is None or identity_alive(identity) or group_alive(identity["pid"])):
                    return False
            data.update(active=False, process=None)
            replace_json(guard.path, data)
    return True

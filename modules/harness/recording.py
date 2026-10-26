"""Exclusive call allocation and idempotent recording-only retries."""
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import uuid

HARNESS_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = {"request.json", "authorization.json", "authorization-source.txt", "instructions.txt", "instructions.json",
             "response-schema.json", "prompt.txt", "preflight.json", "stdout.txt", "stderr.txt",
             "response.json", "result.json"}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def call_path(root, run_id, call_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id):
        raise ValueError("invalid run_id")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", call_id):
        raise ValueError("invalid call_id")
    root = root.resolve()
    current = root
    for part in ("runs", run_id, "calls", "call-" + call_id):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink in recording path: {current}")
    if not current.resolve().is_relative_to(root / "runs"):
        raise ValueError("recording path escapes runs root")
    return current


def write_once(path, data):
    """Commit complete bytes by hard link; a partial write never becomes an artifact."""
    if path.is_symlink():
        raise OSError(f"refusing symlink artifact: {path}")
    if path.exists():
        if path.read_bytes() != data:
            raise OSError(f"recording conflict: {path}")
        return
    temp = path.parent / (".pending-" + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise OSError(f"recording conflict: {path}")
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # A committed artifact remains committed.


@dataclass
class CallOutcome:
    result: dict
    call_dir: Path | None = None
    owner: str | None = None
    pending: dict = field(default_factory=dict, repr=False)
    completed_result: dict | None = field(default=None, repr=False)

    def persist(self):
        if self.call_dir is None:
            return self
        desired = self.completed_result or self.result
        self.pending["result.json"] = json_bytes(desired)
        try:
            if (self.call_dir / ".owner").read_text() != self.owner:
                raise OSError("call directory ownership marker differs")
            # result.json is always the last commit.
            for name, data in self.pending.items():
                if name != "result.json":
                    write_once(self.call_dir / name, data)
            write_once(self.call_dir / "result.json", self.pending["result.json"])
            self.result = desired
            self.completed_result = None
            self.pending.clear()
        except OSError as exc:
            self.completed_result = desired
            self.result = {**desired, "call_status": "recording_error",
                           "error": {"code": "recording_failed", "message": str(exc)},
                           "underlying_call_status": desired["call_status"],
                           "underlying_error": desired.get("error")}
        return self

    def as_dict(self):
        result = dict(self.result)
        if self.completed_result is not None:
            result["recording_retry"] = {
                "owner": self.owner, "result": self.completed_result,
                "artifacts": {k: base64.b64encode(v).decode("ascii") for k, v in self.pending.items()},
            }
        return result


def allocate(root, request):
    path = call_path(root, request["run_id"], request["call_id"])
    workspace = Path(request["workspace"]).resolve()
    if (root.resolve() / "runs").is_relative_to(workspace) or workspace.is_relative_to(root.resolve() / "runs"):
        raise ValueError("M3 requires runs and the target workspace to be separate")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    owner = uuid.uuid4().hex
    write_once(path / ".owner", owner.encode())
    return path, owner


def retry_recording(payload, root=HARNESS_ROOT):
    """A recovery receipt is caller-owned; it is never submitted to a model."""
    retry = payload["recording_retry"]
    result = retry["result"]
    path = call_path(root, result["run_id"], result["call_id"])
    if not path.is_dir() or (path / ".owner").is_symlink():
        raise ValueError("missing original call directory")
    files = retry["artifacts"]
    if not files or not set(files).issubset(ARTIFACTS):
        raise ValueError("invalid recovery artifacts")
    pending = {k: base64.b64decode(v, validate=True) for k, v in files.items()}
    if pending.get("result.json") != json_bytes(result):
        raise ValueError("recovery result does not match artifact")
    return CallOutcome(result, path, retry["owner"], pending).persist()

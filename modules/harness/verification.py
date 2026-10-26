"""Read-only, network-isolated test execution and reproducible workspace evidence."""
import os
from pathlib import Path
import shutil
import stat

from .contracts import ContractError
from .process_runner import run_process
from .recording import json_bytes
from .run_store import digest

# Minimal bootstrap for the currently supported Linux x86-64 environment.
# These are individual runtime files, never a host filesystem/root mount.
BOOTSTRAP_PATHS = ("/usr/bin/true", "/lib64/ld-linux-x86-64.so.2", "/lib/x86_64-linux-gnu/libc.so.6")
PRIVATE_ROOTS = ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp")
SENSITIVE_NAMES = {".codex", ".claude", ".aws", ".ssh", ".gnupg"}


def validate_runtime_paths(paths, workspace, controller_root=None):
    """Caller-reviewed runtime/dependency mounts, bound into an otherwise empty root."""
    workspace = Path(workspace).resolve()
    if type(paths) is not list or any(type(value) is not str for value in paths):
        raise ContractError("runtime_paths must be an explicit list")
    if len(set(paths)) != len(paths):
        raise ContractError("duplicate runtime path")
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ContractError("runtime path must be a normalized absolute path")
        resolved = path.resolve(strict=True)
        if not (resolved.is_file() or resolved.is_dir()):
            raise ContractError("runtime path must be a regular file or directory")
        for candidate in (path, resolved):
            if SENSITIVE_NAMES.intersection(candidate.parts):
                raise ContractError("credential directories are not verification runtime")
            if candidate in (Path("/"), Path("/etc"), Path("/usr"), Path("/var"), Path("/opt")):
                raise ContractError("runtime mount is too broad")
            if Path.home().is_relative_to(candidate):
                raise ContractError("host home cannot be exposed as runtime")
            if candidate.is_relative_to(workspace) or workspace.is_relative_to(candidate):
                raise ContractError("runtime mount overlaps target workspace")
            if controller_root is not None:
                control = Path(controller_root).resolve()
                if candidate.is_relative_to(control) or control.is_relative_to(candidate):
                    raise ContractError("runtime mount overlaps controller")
            if any(candidate.is_relative_to(Path(base)) or Path(base).is_relative_to(candidate)
                   for base in PRIVATE_ROOTS):
                raise ContractError("runtime mount overlaps private sandbox namespace")
    return paths


def snapshot(workspace):
    workspace = Path(workspace)
    entries = {}
    for path in sorted(workspace.rglob("*")):
        name = path.relative_to(workspace).as_posix()
        mode = path.lstat().st_mode
        value = {"mode": mode}
        if stat.S_ISLNK(mode):
            value["link"] = str(path.readlink())
        elif stat.S_ISREG(mode):
            value["sha256"] = digest(path.read_bytes())
        elif not stat.S_ISDIR(mode):
            raise ContractError("unsupported workspace file type: " + name)
        entries[name] = value
    return {"state_id": digest(json_bytes(entries)), "entries": entries}


def changed(before, after):
    a, b = before["entries"], after["entries"]
    return sorted(path for path in set(a) | set(b) if a.get(path) != b.get(path))


def under(path, allowed):
    return any(path == base or path.startswith(base.rstrip("/") + "/") for base in allowed)


def violations(before, after, allowed, protected):
    return [p for p in changed(before, after) if not under(p, allowed) or under(p, protected)]


class VerificationRunner:
    def __init__(self, workspace, *, runner=run_process):
        self.workspace = Path(workspace).resolve()
        self.runner = runner

    def command(self, argv, cwd, runtime_paths):
        executable = shutil.which("bwrap")
        if not executable:
            raise ContractError("unsupported_configuration: bubblewrap unavailable")
        validate_runtime_paths(runtime_paths, self.workspace)
        mounts = dict.fromkeys((*BOOTSTRAP_PATHS, *runtime_paths))
        command = [executable, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                   "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev",
                   "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
        for value in sorted(mounts, key=lambda p: (len(Path(p).parts), p)):
            source = Path(value).resolve(strict=True)
            command += ["--ro-bind", str(source), value]
        # Targets may live under /tmp; bind back only the authorized workspace.
        command += ["--ro-bind", str(self.workspace), str(self.workspace), "--remount-ro", "/",
                    "--chdir", str(cwd), "--", *argv]
        return command

    def run(self, command, budget):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp",
               "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "LOG_BY_HARNESS": "1"}
        return budget.wrap(self.runner)(self.command(command["argv"], command["cwd"], command["runtime_paths"]),
                                        cwd=self.workspace, env=env, timeout_seconds=command["timeout_seconds"])

    def probe(self, budget):
        return self.run({"argv": ["/usr/bin/true"], "cwd": str(self.workspace), "timeout_seconds": 10,
                         "runtime_paths": []}, budget)


FAILURE_FLAGS = ("timed_out", "cancelled", "output_limit_exceeded", "post_exit_drain_expired",
                 "descendants_terminated", "spawn_error", "io_error")


def metadata_ok(metadata):
    """One pass rule for a live result and for its recorded metadata."""
    return (metadata.get("returncode") == 0 and metadata.get("cleanup_completed") is True
            and not any(metadata.get(flag) for flag in FAILURE_FLAGS))


def process_ok(result):
    return metadata_ok(result.metadata())

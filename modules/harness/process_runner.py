"""Bounded POSIX subprocess execution without shell interpolation or retries."""
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

OUTPUT_LIMIT = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 3600
POST_EXIT_DRAIN_SECONDS = 2


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProcessResult:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int | None = None
    timed_out: bool = False
    output_limit_exceeded: bool = False
    cleanup_completed: bool = True
    descendants_terminated: bool = False
    post_exit_drain_expired: bool = False
    run_deadline_expired: bool = False
    cancelled: bool = False
    effective_timeout_seconds: float | None = None
    spawn_error: str | None = None
    io_error: str | None = None
    started_at: str = ""
    ended_at: str = ""
    elapsed_seconds: float = 0
    termination_grace_seconds: float = 5
    post_exit_drain_seconds: float = POST_EXIT_DRAIN_SECONDS

    def metadata(self):
        return {key: value for key, value in vars(self).items() if key not in {"stdout", "stderr"}}


def group_alive(pgid):
    """Linux: unreaped zombies cannot execute or write and do not count as live."""
    if Path("/proc").is_dir():
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == pgid and fields[0] != "Z":
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def terminate_group(process, grace):
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            process.poll()
            if not group_alive(process.pid):
                break
            time.sleep(0.02)
        if not group_alive(process.pid):
            break
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return False
    return not group_alive(process.pid)


def run_process(argv, *, cwd, stdin=b"", env=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                output_limit=OUTPUT_LIMIT, termination_grace_seconds=5,
                post_exit_drain_seconds=POST_EXIT_DRAIN_SECONDS, deadline_monotonic=None,
                cancel_check=None, on_start=None):
    if os.name != "posix":
        raise ValueError("the M3 process runner requires POSIX process groups")
    if (not argv or timeout_seconds <= 0 or output_limit < 1
            or termination_grace_seconds < 0 or post_exit_drain_seconds < 0):
        raise ValueError("argv, positive timeout and output limit are required")
    result = ProcessResult(started_at=now(), termination_grace_seconds=termination_grace_seconds,
                           post_exit_drain_seconds=post_exit_drain_seconds)
    started = time.monotonic()
    result.effective_timeout_seconds = min(timeout_seconds, max(0, deadline_monotonic - started)) if deadline_monotonic is not None else timeout_seconds
    if deadline_monotonic is not None and started >= deadline_monotonic:
        result.timed_out = result.run_deadline_expired = True
        result.ended_at = now()
        return result
    if cancel_check and cancel_check():
        result.cancelled = True
        result.ended_at = now()
        return result
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, shell=False)
    except (OSError, ValueError) as exc:
        result.spawn_error = str(exc)
        result.ended_at = now()
        result.elapsed_seconds = time.monotonic() - started
        return result
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    offset = 0
    deadline = started + timeout_seconds
    drain_deadline = None
    try:
        if on_start:
            on_start(process)
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if stdin:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while True:
            # Always poll, even while descendants hold stdout/stderr open.
            exited = process.poll() is not None
            current = time.monotonic()
            if cancel_check and cancel_check():
                result.cancelled = True
                break
            if deadline_monotonic is not None and current >= deadline_monotonic:
                result.timed_out = result.run_deadline_expired = True
                break
            if exited:
                if not selector.get_map():
                    break
                if drain_deadline is None:
                    drain_deadline = current + post_exit_drain_seconds
                if current >= drain_deadline:
                    result.post_exit_drain_expired = True
                    break
                wait_deadline = drain_deadline
            else:
                if current >= deadline:
                    result.timed_out = True
                    break
                wait_deadline = deadline
            if deadline_monotonic is not None:
                wait_deadline = min(wait_deadline, deadline_monotonic)
            for key, _ in selector.select(min(0.05, max(0, wait_deadline - time.monotonic()))):
                if key.data == "stdin":
                    try:
                        offset += os.write(key.fd, stdin[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(stdin)
                    if offset == len(stdin):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buf = buffers[key.data]
                available = output_limit - len(buf)
                buf.extend(data[:available])
                if len(data) > available:
                    result.output_limit_exceeded = True
            if result.output_limit_exceeded:
                break
    except OSError as exc:
        result.io_error = str(exc)
    finally:
        # Covers deadlines, I/O failures, exceptions and descendants holding pipes.
        if group_alive(process.pid):
            result.descendants_terminated = process.poll() is not None
            result.cleanup_completed = terminate_group(process, termination_grace_seconds)
        else:
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        selector.close()
        result.stdout, result.stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])
        result.returncode = process.returncode
        result.ended_at = now()
        result.elapsed_seconds = time.monotonic() - started
    return result

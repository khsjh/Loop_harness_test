"""Shared adapter errors and bounded final-file reader."""
import os
import stat

from ..contracts import ContractError
from ..process_runner import OUTPUT_LIMIT


class AdapterError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def read_final(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ContractError("agent final output is not a regular file")
            data = file.read(OUTPUT_LIMIT + 1)
    except OSError as exc:
        raise ContractError(f"cannot read fresh agent final file: {exc}") from exc
    if len(data) > OUTPUT_LIMIT:
        raise AdapterError("output_limit_exceeded", "final_output_limit", "agent final file exceeds 8 MiB")
    return data


def model_metadata(reports):
    unique = list(dict.fromkeys(r["model"] for r in reports))
    return {"actual_model": unique[0] if len(unique) == 1 else "unknown",
            "reported_models": reports}

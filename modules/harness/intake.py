"""Capture the verbatim user message, never a rewritten objective or task summary.

Rewritten work instructions belong in the run request's objective. This capture
preserves bytes; the trusted caller remains responsible for sourcing the original.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import uuid

from .contracts import ContractError, nonempty
from .process_runner import now
from .recording import HARNESS_ROOT
from .run_store import commit, digest, safe_path, sync_directory


def capture(question, *, root=HARNESS_ROOT, source_kind="manual_import", source_event_id=None,
            user_asked_at=None, authorization_id=None):
    nonempty(question, "user input")
    if source_kind not in {"direct_stdin", "manual_import"}:
        raise ContractError("unknown input source kind")
    identity = authorization_id or "input-" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", identity):
        raise ContractError("invalid input identifier")
    directory = safe_path(root, "approvals", identity)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    sync_directory(directory.parent)
    message = {"schema_version": 1, "message_id": identity, "role": "user",
               "content": question, "user_asked_at": user_asked_at}
    path = directory / "user-message.json"
    commit(path, message)
    source = {"kind": "user_message", "path": str(path), "message_id": identity,
              "sha256": digest(path.read_bytes())}
    metadata = {"schema_version": 1, "source_kind": source_kind, "source_event_id": source_event_id,
                "received_at": now(), "source": source}
    commit(directory / "intake.json", metadata)
    return str(directory / "intake.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdin", action="store_true")
    mode.add_argument("--question-file", type=Path)
    parser.add_argument("--source-event-id")
    parser.add_argument("--user-asked-at")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read() if args.stdin else args.question_file.read_bytes()
    print(json.dumps({"input_ref": capture(raw.decode("utf-8"), source_kind="direct_stdin" if args.stdin else "manual_import",
                                           source_event_id=args.source_event_id, user_asked_at=args.user_asked_at),
                      "approved": False}))


if __name__ == "__main__":
    main()

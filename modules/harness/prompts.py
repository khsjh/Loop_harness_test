"""Snapshot target instructions and carry the caller's authorization context."""
import hashlib
import json
import os
from pathlib import Path

from .contracts import ContractError, response_schema


def snapshot_instructions(request):
    workspace = Path(request["workspace"]).resolve()
    required = set()
    for directory in (workspace, *workspace.parents):
        for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
            path = directory / name
            if path.exists():
                required.add(path.resolve())
    # Conservatively require all nested rules, including rules for newly created files.
    for root, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
            if name in files:
                required.add((Path(root) / name).resolve())
    supplied = {Path(p).resolve() for p in request["instruction_files"]}
    if required - supplied:
        raise ContractError(f"missing target instructions: {sorted(map(str, required - supplied))}")
    snapshots = []
    for name in request["instruction_files"]:
        try:
            data = Path(name).read_bytes()
            if len(data) > 1024 * 1024:
                raise ContractError(f"instruction file exceeds 1 MiB: {name}")
            text = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContractError(f"cannot read instruction file: {name}: {exc}") from exc
        snapshots.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "text": text})
    return snapshots


def build_prompt(request, authorization, instructions):
    role = request["role"]
    role_text = (
        "Implement only the authorized task within allowed_paths. Report actual changes. "
        "complete is a self-report, not test or review approval."
        if role == "implementer" else
        "Review only. Do not create, edit, delete or execute files. Report required fixes "
        "as findings; approved requires no findings."
    )
    return (
        "This is one harness call, not an autonomous loop. " + role_text + "\n"
        "The caller has checked the following original user instruction and its exact delegated "
        "scope. It authorizes only that scope, not arbitrary future actions. "
        "Ask via needs_user if the task requires a new decision.\n"
        "The harness records this call (LOG_BY_HARNESS=1). Do not write dialog or runs files. "
        "Do not launch other agents. Verification commands are context for separate harness "
        "verification; your own claims are not independent test evidence.\n"
        "Return only the role response JSON. Use nonempty text; relative workspace paths "
        "without '..'; line numbers are positive or null. complete has no remaining_work; "
        "incomplete requires remaining_work. approved has no findings; changes_requested "
        "requires findings. Only needs_user has nonempty questions.\n\n"
        + json.dumps({"authorization_ref": request["authorization_ref"],
                      "authorization": authorization, "request": request,
                      "target_instructions": instructions,
                      "response_schema": response_schema(request)}, ensure_ascii=False, indent=2)
        + "\n"
    )

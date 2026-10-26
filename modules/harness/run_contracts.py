"""Strict M5 requests and caller-owned loop approvals. No default authorization."""
from datetime import datetime
import json
from pathlib import Path
import re

from .adapters import claude, codex
from .authorization import resolve_source
from .contracts import (ContractError, STRING, STRINGS, VERSION, array, obj, structure,
                        loads, nonempty, relative_path, validate_request)
from .prompts import snapshot_instructions
from .recording import json_bytes
from .run_store import digest, safe_path
from .verification import validate_runtime_paths
from .run_policy import DEFAULT_LIMITS, DEFAULT_TIMEOUT_POLICY

ASSIGNMENT = obj({"agent": {"type": "string", "enum": ["codex", "claude"]},
                  "model": STRING, "reasoning_effort": STRING})
CALLER_DIALOG = obj({"message_id": STRING, "call_id": STRING})
CALLER_DIALOG["type"] = ["object", "null"]
RUN_SCHEMA = obj({
    "schema_version": VERSION, "run_id": STRING, "parent_run_id": {"type": ["string", "null"]},
    "input_ref": STRING, "authorization_ref": STRING, "objective": STRING, "workspace": STRING,
    "allowed_paths": STRINGS, "instruction_files": STRINGS,
    "assignments": obj({"implementer": ASSIGNMENT, "reviewer": ASSIGNMENT}),
    "verification_commands": array(obj({"argv": STRINGS, "cwd": STRING, "timeout_seconds": {"type": "integer"},
                                        "protected_paths": STRINGS, "runtime_paths": STRINGS})),
    "limits": obj({"max_rounds": {"type": "integer"}, "run_timeout_seconds": {"type": "integer"},
                   "call_timeout_seconds": {"type": "integer"}}),
    "timeout_policy": obj({"max_test_timeout_seconds": {"type": "integer"},
                           "max_consecutive_timeouts": {"type": "integer"}}),
    "dialog_owner": {"type": "string", "enum": ["harness", "caller"]},
    "caller_dialog": CALLER_DIALOG,
    "report_identity": obj({"ai": {"type": "string", "enum": ["codex", "claude", "harness"]}, "model": STRING}),
})
RUN_SCOPE = tuple(k for k in RUN_SCHEMA["properties"] if k not in {"schema_version", "run_id", "parent_run_id", "authorization_ref"})
TEMPLATE_VERSION = "m5-v5"


def identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ContractError("invalid identifier")
    return value


def approved_scope(request):
    return {key: request[key] for key in RUN_SCOPE}


def call_request(request, role, round_number, task):
    assignment = request["assignments"][role]
    return {"schema_version": 2, "run_id": request["run_id"], "call_id": f"r{round_number}-{role}",
            "round": round_number, **assignment, "role": role, "authorization_ref": request["authorization_ref"],
            "task": task, "workspace": request["workspace"],
            "allowed_paths": request["allowed_paths"] if role == "implementer" else [],
            "instruction_files": request["instruction_files"],
            "verification_commands": [command["argv"] for command in request["verification_commands"]],
            "timeout_seconds": request["limits"]["call_timeout_seconds"]}


def validate_run(request, root):
    structure(request, RUN_SCHEMA)
    for key in ("run_id", "authorization_ref"):
        identifier(request[key])
    if request["parent_run_id"] is not None:
        identifier(request["parent_run_id"])
        if request["parent_run_id"] == request["run_id"]:
            raise ContractError("run cannot be its own parent")
    for key in ("objective", "input_ref", "workspace"):
        nonempty(request[key], key)
    nonempty(request["report_identity"]["model"], "report model")
    identity = request["report_identity"]
    if request["dialog_owner"] == "harness":
        if identity != {"ai": "harness", "model": "none"}:
            raise ContractError("harness-owned dialog requires ai=harness, model=none")
        if request["caller_dialog"] is not None:
            raise ContractError("harness-owned answers cannot share a caller dialog")
    elif identity["ai"] not in {"codex", "claude"}:
        raise ContractError("caller-owned dialog requires the actual caller identity")
    else:
        if identity["model"] == "none":
            raise ContractError("model=none is reserved for harness answers")
        if request["caller_dialog"] is None:
            raise ContractError("caller-owned requests require an explicit shared dialog reference")
        for value in request["caller_dialog"].values():
            identifier(value)
    if any(value < 1 for value in request["limits"].values()):
        raise ContractError("positive execution limits required")
    if any(value > DEFAULT_LIMITS[key] for key, value in request["limits"].items()):
        raise ContractError("execution limits exceed the approved 24h / 6 rounds / 6h ceilings")
    policy = request["timeout_policy"]
    if not 1 <= policy["max_test_timeout_seconds"] <= DEFAULT_TIMEOUT_POLICY["max_test_timeout_seconds"]:
        raise ContractError("test retry timeout must be within 1..21600 seconds")
    if not 1 <= policy["max_consecutive_timeouts"] <= DEFAULT_TIMEOUT_POLICY["max_consecutive_timeouts"]:
        raise ContractError("consecutive test timeouts must be within 1..3 attempts")
    ws = Path(request["workspace"])
    if not ws.is_absolute() or not ws.is_dir() or str(ws.resolve()) != str(ws):
        raise ContractError("workspace must be an existing canonical absolute directory")
    for directory in ("approvals", "runs", "dialog"):
        control = safe_path(root, directory)
        if ws.is_relative_to(control) or control.is_relative_to(ws):
            raise ContractError("workspace overlaps controller data")
    if request["assignments"]["implementer"]["agent"] == request["assignments"]["reviewer"]["agent"]:
        raise ContractError("implementer and reviewer must use different agents")
    for role in ("implementer", "reviewer"):
        call = call_request(request, role, 1, request["objective"])
        validate_request(call)
        (codex if call["agent"] == "codex" else claude).check_configuration(call)
    if not request["verification_commands"]:
        raise ContractError("independent verification required")
    for command in request["verification_commands"]:
        validate_runtime_paths(command["runtime_paths"], ws, root)
        if not command["argv"] or not command["protected_paths"] or command["timeout_seconds"] < 1:
            raise ContractError("verification argv, protected inputs and positive timeout required")
        if command["timeout_seconds"] > policy["max_test_timeout_seconds"]:
            raise ContractError("initial test timeout exceeds the approved test ceiling")
        for arg in command["argv"]:
            nonempty(arg, "verification argument")
        cwd = Path(command["cwd"])
        if not cwd.is_absolute() or not cwd.is_dir() or not cwd.resolve().is_relative_to(ws):
            raise ContractError("verification cwd outside workspace")
        for path in command["protected_paths"]:
            if not relative_path(path, ws).exists():
                raise ContractError("protected verification input missing")
    return snapshot_instructions(call_request(request, "implementer", 1, request["objective"]))


def load_approval(request, path, root):
    """The caller must assess the instruction before supplying this exact record."""
    path = Path(path)
    approvals = safe_path(root, "approvals")
    if not path.is_absolute() or not path.resolve().is_relative_to(approvals):
        raise ContractError("run approval must be under controller approvals/")
    path = safe_path(root, str(path.relative_to(Path(root).resolve())))
    entry = loads(path.read_bytes())
    if type(entry) is not dict or set(entry) != {"schema_version", "authorization_ref", "user_instruction", "source",
                                               "scope", "instruction_hashes", "intake_sha256", "allow_iterations", "template_version"}:
        raise ContractError("invalid run approval fields")
    if type(entry["schema_version"]) is not int or entry["schema_version"] != 1:
        raise ContractError("unsupported approval version")
    if entry["authorization_ref"] != request["authorization_ref"] or entry["allow_iterations"] is not True:
        raise ContractError("loop iteration authorization missing")
    if (entry["template_version"] != TEMPLATE_VERSION or
            json.dumps(entry["scope"], sort_keys=True) != json.dumps(approved_scope(request), sort_keys=True)):
        raise ContractError("run request differs from caller-approved scope")
    nonempty(entry["user_instruction"], "original user instruction")
    source = entry["source"]
    if type(source) is not dict or source.get("kind") != "user_message":
        raise ContractError("M5 requires captured user_message input")
    source_path = Path(source.get("path", ""))
    if not source_path.is_absolute() or not source_path.is_relative_to(approvals):
        raise ContractError("M5 input source must be under controller approvals/")
    safe_path(root, str(source_path.relative_to(Path(root).resolve())))
    snapshot = resolve_source(entry["source"], entry["user_instruction"], request["workspace"])
    intake_path = Path(request["input_ref"])
    if not intake_path.is_absolute() or not intake_path.is_relative_to(approvals):
        raise ContractError("intake must be under approvals/")
    intake_path = safe_path(root, str(intake_path.relative_to(Path(root).resolve())))
    intake_raw = intake_path.read_bytes()
    intake = loads(intake_raw)
    if (type(intake) is not dict or set(intake) != {"schema_version", "source_kind", "source_event_id", "received_at", "source"}
            or type(intake["schema_version"]) is not int or intake["schema_version"] != 1
            or intake["source_kind"] not in ("manual_import", "direct_stdin")
            or intake["source"] != entry["source"] or digest(intake_raw) != entry["intake_sha256"]):
        raise ContractError("intake identity/hash mismatch")
    message = loads(snapshot)
    if request["caller_dialog"] is not None and request["caller_dialog"]["message_id"] != message["message_id"]:
        raise ContractError("caller dialog message_id differs from the original intake")
    asked = message["user_asked_at"]
    if asked is not None:
        parsed = datetime.fromisoformat(asked)
        if parsed.tzinfo is None:
            raise ContractError("user_asked_at requires explicit timezone")
    return entry, message


def check_rules(request, approval):
    rules = snapshot_instructions(call_request(request, "implementer", 1, request["objective"]))
    if {item["path"]: item["sha256"] for item in rules} != approval["instruction_hashes"]:
        raise ContractError("instruction hashes changed")
    return rules

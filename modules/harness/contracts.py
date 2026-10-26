"""Version 1 structures and semantic validation; no third-party dependencies."""
import copy
import json
import math
from pathlib import Path, PurePosixPath
import re


class ContractError(ValueError):
    pass


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ContractError(f"non-finite JSON number: {value}")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ContractError("non-finite JSON number")
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                          parse_float=number)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError(str(exc)) from exc


def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}
STRINGS = array(STRING)
VERSION = {"type": "integer", "enum": [1]}
REQUEST_SCHEMA = obj({
    "schema_version": {"type": "integer", "enum": [1, 2]}, "run_id": STRING, "call_id": STRING,
    "round": {"type": ["integer", "null"]},
    "agent": {"type": "string", "enum": ["codex", "claude"]},
    "role": {"type": "string", "enum": ["implementer", "reviewer"]},
    "model": STRING, "reasoning_effort": STRING, "authorization_ref": STRING,
    "task": STRING, "workspace": STRING, "allowed_paths": STRINGS,
    "instruction_files": STRINGS, "verification_commands": array(STRINGS),
    "timeout_seconds": {"type": "integer"},
})
RESPONSE_SCHEMAS = {
    "implementer": obj({
        "schema_version": VERSION,
        "status": {"type": "string", "enum": ["complete", "incomplete", "needs_user"]},
        "summary": STRING, "changed_files": STRINGS,
        "remaining_work": STRINGS, "questions": STRINGS,
    }),
    "reviewer": obj({
        "schema_version": VERSION,
        "verdict": {"type": "string", "enum": ["approved", "changes_requested", "needs_user"]},
        "summary": STRING,
        "findings": array(obj({
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "path": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"]}, "message": STRING,
        })), "questions": STRINGS,
    }),
}
RESPONSE_SCHEMAS_V2 = copy.deepcopy(RESPONSE_SCHEMAS)
for schema in RESPONSE_SCHEMAS_V2.values():
    schema["properties"]["schema_version"] = {"type": "integer", "enum": [2]}
TIMEOUT_RECOVERY_SCHEMA = obj({
    "action": {"type": "string", "enum": ["retry", "stop"]},
    "reason": STRING, "plan": STRING,
    "test_timeout_seconds": {"type": "integer"},
    "estimated_seconds": {"type": "integer"}, "required_rounds": {"type": "integer"},
})
TIMEOUT_RECOVERY_SCHEMA["type"] = ["object", "null"]
RESPONSE_SCHEMAS_V2["implementer"]["properties"]["timeout_recovery"] = TIMEOUT_RECOVERY_SCHEMA
RESPONSE_SCHEMAS_V2["implementer"]["required"].append("timeout_recovery")


def response_schema(request):
    schemas = RESPONSE_SCHEMAS_V2 if request["schema_version"] == 2 else RESPONSE_SCHEMAS
    return schemas[request["role"]]


# A caller-controlled registry binds every substantive field, not just a label.
SCOPE_FIELDS = tuple(k for k in REQUEST_SCHEMA["properties"]
                     if k not in {"schema_version", "run_id", "call_id", "round", "authorization_ref"})


def request_scope(request):
    value = {key: request[key] for key in SCOPE_FIELDS}
    # Version 2 changes response semantics; an approval cannot be downgraded to v1.
    if request["schema_version"] == 2:
        value["schema_version"] = 2
    return value


def structure(value, schema, location="$"):
    types = schema["type"]
    if isinstance(types, str):
        types = [types]
    names = {dict: "object", list: "array", str: "string", int: "integer", type(None): "null"}
    if names.get(type(value)) not in types:
        raise ContractError(f"{location}: expected {'/'.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{location}: unsupported value")
    if type(value) is dict:
        expected = set(schema["properties"])
        if set(value) != expected:
            raise ContractError(f"{location}: missing {sorted(expected - set(value))}, "
                                f"unknown {sorted(set(value) - expected)}")
        for key, child in schema["properties"].items():
            structure(value[key], child, f"{location}.{key}")
    elif type(value) is list:
        for i, child in enumerate(value):
            structure(child, schema["items"], f"{location}[{i}]")


def nonempty(value, label):
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ContractError(f"{label}: expected nonempty text without NUL")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ContractError(f"{label}: invalid Unicode") from exc


def relative_path(value, workspace):
    nonempty(value, "path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value or value == ".":
        raise ContractError(f"unsafe relative path: {value}")
    try:
        resolved = (workspace / value).resolve()
    except (OSError, RuntimeError) as exc:
        raise ContractError(f"cannot resolve path: {value}") from exc
    if not resolved.is_relative_to(workspace):
        raise ContractError(f"path escapes workspace: {value}")
    return resolved


def validate_request(request):
    structure(request, REQUEST_SCHEMA)
    for key in ("model", "reasoning_effort", "authorization_ref", "task", "workspace"):
        nonempty(request[key], key)
    if request["model"] == "none":
        raise ContractError("model=none is reserved for harness template answers")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request["run_id"]):
        raise ContractError("invalid run_id")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request["call_id"]):
        raise ContractError("invalid call_id")
    if request["timeout_seconds"] < 1 or (request["round"] is not None and request["round"] < 1):
        raise ContractError("timeout_seconds and non-null round must be positive")
    workspace = Path(request["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ContractError("workspace must be an existing absolute directory")
    workspace = workspace.resolve()
    paths = request["allowed_paths"]
    if request["role"] == "reviewer" and paths:
        raise ContractError("reviewer allowed_paths must be empty")
    if request["role"] == "implementer" and not paths:
        raise ContractError("implementer needs allowed_paths")
    for path in paths:
        relative_path(path, workspace)
    for path in request["instruction_files"]:
        nonempty(path, "instruction_files")
        file = Path(path)
        if not file.is_absolute() or not file.is_file():
            raise ContractError(f"instruction file must exist and be absolute: {path}")
        file = file.resolve()
        if not (file.is_relative_to(workspace) or workspace.is_relative_to(file.parent)):
            raise ContractError(f"instruction file is unrelated to workspace: {path}")
    for command in request["verification_commands"]:
        if not command:
            raise ContractError("verification command must not be empty")
        for arg in command:
            if "\0" in arg:
                raise ContractError("NUL in command argument")
        nonempty(command[0], "command executable")
    return workspace


def validate_authorization(request, registry):
    if type(registry) is not dict:
        raise ContractError("authorization registry must be an object")
    entry = registry.get(request["authorization_ref"])
    if type(entry) is not dict or set(entry) != {"user_instruction", "scope", "source"}:
        raise ContractError("authorization reference has no verified caller record")
    nonempty(entry["user_instruction"], "original user instruction")
    expected = request_scope(request)
    # Compare canonical JSON so True and 1 are never equivalent.
    if json.dumps(entry["scope"], sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ContractError("request differs from the caller-approved scope")
    from .authorization import resolve_source
    snapshot = resolve_source(entry["source"], entry["user_instruction"], request["workspace"])
    return {**entry, "source_snapshot": snapshot}


def validate_response(response, request):
    structure(response, response_schema(request))
    nonempty(response["summary"], "summary")
    for question in response["questions"]:
        nonempty(question, "question")
    workspace = Path(request["workspace"]).resolve()
    state = response.get("status", response.get("verdict"))
    if bool(response["questions"]) != (state == "needs_user"):
        raise ContractError("questions required only for needs_user")
    if request["role"] == "implementer":
        recovery = response.get("timeout_recovery")
        if recovery is not None:
            nonempty(recovery["reason"], "timeout recovery reason")
            nonempty(recovery["plan"], "timeout recovery plan")
            if recovery["action"] == "retry":
                if min(recovery[k] for k in ("test_timeout_seconds", "estimated_seconds", "required_rounds")) < 1:
                    raise ContractError("timeout retry requires positive resource estimates")
                if recovery["estimated_seconds"] < recovery["test_timeout_seconds"]:
                    raise ContractError("recovery estimate must include the requested test budget")
            elif any(recovery[k] != 0 for k in ("test_timeout_seconds", "estimated_seconds", "required_rounds")):
                raise ContractError("stop decision must not request more resources")
        for item in response["remaining_work"]:
            nonempty(item, "remaining_work")
        if state == "complete" and response["remaining_work"]:
            raise ContractError("complete cannot have remaining_work")
        if state == "incomplete" and not response["remaining_work"]:
            raise ContractError("incomplete requires remaining_work")
        allowed = [relative_path(p, workspace) for p in request["allowed_paths"]]
        for item in response["changed_files"]:
            path = relative_path(item, workspace)
            if not any(path == p or path.is_relative_to(p) for p in allowed):
                raise ContractError(f"reported change outside allowed_paths: {item}")
    else:
        if state == "approved" and response["findings"]:
            raise ContractError("approved cannot have findings")
        if state == "changes_requested" and not response["findings"]:
            raise ContractError("changes_requested requires findings")
        for finding in response["findings"]:
            nonempty(finding["message"], "finding message")
            if finding["path"] is None:
                if finding["line"] is not None:
                    raise ContractError("finding without path cannot have line")
            else:
                relative_path(finding["path"], workspace)
            if finding["line"] is not None and finding["line"] < 1:
                raise ContractError("finding line must be positive")
    return response

"""Run one explicitly authorized CLI call; no automatic model retries.

python3 -m modules.harness.run_agent_once --request request.json --authorizations approvals.json
python3 -m modules.harness.run_agent_once --retry-recording receipt.json
"""
import argparse
import json
import os
from pathlib import Path
import sys

from .adapters import claude, codex
from .adapters.common import AdapterError
from .authorization import load_authorizations
from .contracts import (ContractError, response_schema, loads, validate_authorization,
                        validate_request, validate_response)
from .process_runner import OUTPUT_LIMIT, run_process
from .prompts import build_prompt, snapshot_instructions
from .recording import (HARNESS_ROOT, CallOutcome, allocate, json_bytes, retry_recording,
                        write_once)


def failed(result, status, code, message):
    result.update(call_status=status, error={"code": code, "message": message})
    return result


def base_result(request):
    value = request if type(request) is dict else {}
    return {"schema_version": 1, **{k: value.get(k) for k in ("run_id", "call_id", "agent", "role")},
            "call_status": "invalid_request", "error": None, "process": None,
            "response": None, "response_valid": False, "actual_model": "unknown",
            "reported_models": [], "artifacts": {}, "execution": {}}


def preflight(adapter, executable, workspace, runner):
    evidence = {}
    commands = [[executable, "--version"], [executable, "--help"]]
    if adapter is codex:
        commands.append([executable, "exec", "--help"])
    for i, command in enumerate(commands):
        probe = runner(command, cwd=workspace, timeout_seconds=10, output_limit=256 * 1024)
        evidence[str(i)] = {"argv": command, "stdout": probe.stdout.decode("utf-8", "replace"),
                            "stderr": probe.stderr.decode("utf-8", "replace"), **probe.metadata()}
        if probe.spawn_error:
            raise AdapterError("spawn_error", "cli_not_available", probe.spawn_error)
        if probe.run_deadline_expired:
            raise AdapterError("timeout", "run_deadline", "whole-run deadline reached during preflight")
        if probe.cancelled:
            raise AdapterError("process_error", "interrupted", "call interrupted during preflight")
        if (probe.timed_out or probe.output_limit_exceeded or probe.post_exit_drain_expired
                or probe.descendants_terminated or probe.io_error or not probe.cleanup_completed
                or probe.returncode != 0):
            raise AdapterError("unsupported_configuration", "preflight_failed", "CLI help/version probe failed")
    help_text = "\n".join(v["stdout"] for k, v in evidence.items() if k != "0")
    required = adapter.REQUIRED_FLAGS | ({"--ask-for-approval", "--no-daemon"} if adapter is codex else set())
    missing = sorted(flag for flag in required if flag not in help_text)
    if missing:
        raise AdapterError("unsupported_configuration", "missing_cli_flags", ", ".join(missing))
    return evidence


def run_once(request, authorizations, *, harness_root=HARNESS_ROOT, executable=None,
             runner=run_process, check_cli=True, execution_context=None):
    """authorizations must come from the trusted caller, never from model output.

    executable/runner/root/check_cli are dependency injection points for offline tests.
    The public command always checks the installed CLI and anchors records to this package.
    """
    result = base_result(request)
    if execution_context is not None:
        runner = execution_context.wrap(runner)
    outcome = CallOutcome(result)
    try:
        workspace = validate_request(request)
        authorization = validate_authorization(request, authorizations)
        source_snapshot = authorization.pop("source_snapshot")
        instructions = snapshot_instructions(request)
        prompt = build_prompt(request, authorization, instructions)
        if len(prompt.encode("utf-8")) > OUTPUT_LIMIT:
            raise ContractError("combined prompt exceeds 8 MiB")
    except (ContractError, OSError, ValueError, RuntimeError) as exc:
        return CallOutcome(failed(result, "invalid_request", "request_validation", str(exc)))
    adapter = codex if request["agent"] == "codex" else claude
    try:
        adapter.check_configuration(request)
    except AdapterError as exc:
        return CallOutcome(failed(result, exc.status, exc.code, str(exc)))
    try:
        call_dir, owner = allocate(harness_root, request)
        outcome.call_dir, outcome.owner = call_dir, owner
        inputs = {"request.json": json_bytes(request), "authorization.json": json_bytes(authorization),
                  "authorization-source.txt": source_snapshot.encode("utf-8"),
                  "instructions.json": json_bytes(instructions),
                  "instructions.txt": "\n\n".join(f"{x['path']}\n{x['text']}" for x in instructions).encode("utf-8"),
                  "response-schema.json": json_bytes(response_schema(request)),
                  "prompt.txt": prompt.encode("utf-8")}
        for name, data in inputs.items():
            write_once(call_dir / name, data)
        # Establish the output sink before starting a model; raw bytes are buffered until exit.
        write_once(call_dir / ".recording-ready", b"1")
    except (OSError, ValueError) as exc:
        return CallOutcome(failed(result, "recording_error", "recording_start", str(exc)))
    executable = executable or request["agent"]
    argv = adapter.command(request, call_dir, executable)
    result["authorization_ref"] = request["authorization_ref"]
    result["authorization_source"] = authorization["source"]
    result["execution"] = {"argv": argv, "requested_model": request["model"],
                           "reasoning_effort": request["reasoning_effort"],
                           "settings_source": "caller-approved explicit request",
                           "timeout_seconds": request["timeout_seconds"],
                           "output_limit_bytes_per_stream": OUTPUT_LIMIT,
                           "automatic_model_retries": 0, "cli_version": "unknown"}
    result["artifacts"] = {name: str(call_dir / name) for name in inputs}
    env = dict(os.environ)
    env["LOG_BY_HARNESS"] = "1"
    try:
        if check_cli:
            evidence = preflight(adapter, executable, workspace, runner)
            outcome.pending["preflight.json"] = json_bytes(evidence)
            result["execution"]["cli_version"] = evidence["0"]["stdout"].strip()
        process = runner(argv, cwd=workspace, stdin=prompt.encode("utf-8"), env=env,
                         timeout_seconds=request["timeout_seconds"], output_limit=OUTPUT_LIMIT)
        result["process"] = process.metadata()
        result["execution"]["effective_timeout_seconds"] = process.effective_timeout_seconds
        result["execution"]["run_deadline_enforced"] = execution_context is not None
        outcome.pending.update({"stdout.txt": process.stdout, "stderr.txt": process.stderr})
        if process.spawn_error:
            raise AdapterError("spawn_error", "spawn_failed", process.spawn_error)
        if process.timed_out:
            raise AdapterError("timeout", "run_deadline" if process.run_deadline_expired else "deadline_exceeded",
                               "agent call exceeded its execution budget")
        if process.cancelled:
            raise AdapterError("process_error", "interrupted", "agent call interrupted")
        if process.output_limit_exceeded:
            raise AdapterError("output_limit_exceeded", "stream_limit", "agent output exceeded collection limit")
        if process.post_exit_drain_expired:
            raise AdapterError("process_error", "post_exit_drain_expired",
                               "CLI exited but inherited output pipes remained open beyond the drain grace")
        if not process.cleanup_completed or process.descendants_terminated or process.io_error:
            raise AdapterError("process_error", "process_cleanup", "process I/O or descendant cleanup was not normal")
        if process.returncode != 0:
            raise AdapterError("process_error", "nonzero_exit", f"CLI exited with code {process.returncode}")
        response, models = adapter.extract(process.stdout, call_dir)
        result.update(models)
        result["response"] = validate_response(response, request)
        result.update(response_valid=True, call_status="ok", error=None)
        outcome.pending["response.json"] = json_bytes(response)
    except AdapterError as exc:
        failed(result, exc.status, exc.code, str(exc))
    except (ContractError, UnicodeError, OSError, ValueError, RecursionError) as exc:
        failed(result, "invalid_response", "response_validation", str(exc))
    for name in (*outcome.pending, "result.json"):
        result["artifacts"][name] = str(call_dir / name)
    if (call_dir / "agent-final.txt").exists():
        result["artifacts"]["agent-final.txt"] = str(call_dir / "agent-final.txt")
    return outcome.persist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", type=Path)
    mode.add_argument("--retry-recording", type=Path)
    parser.add_argument("--authorizations", type=Path,
                        help="caller-owned approval registry outside the target workspace")
    args = parser.parse_args(argv)
    try:
        if args.retry_recording:
            outcome = retry_recording(loads(args.retry_recording.read_bytes()))
        else:
            request = loads(args.request.read_bytes())
            validate_request(request)
            if not args.authorizations:
                raise ContractError("--authorizations is required")
            registry = load_authorizations(args.authorizations, request["workspace"])
            outcome = run_once(request, registry)
        result = outcome.as_dict()
    except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
        result = failed(base_result(None), "invalid_request", "input_error", str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["call_status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

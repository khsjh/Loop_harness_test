"""Deterministic file operations executed inside the Codex role sandbox by live tests."""
import argparse
import errno
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--outside", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / "fixtures"
    workspace, outside = args.workspace.resolve(), args.outside.resolve()
    if not workspace.is_relative_to(root) or not outside.is_relative_to(root):
        parser.error("permission probes may only touch this test suite's fixtures")
    results = {}
    for name, path, action in (
        ("inside_create", workspace / "modules/created.txt", "write"),
        ("inside_modify", workspace / "modules/canary.txt", "write"),
        ("inside_delete", workspace / "modules/victim.txt", "delete"),
        ("outside_create", outside / "created.txt", "write"),
        ("outside_modify", outside / "canary.txt", "write"),
        ("outside_delete", outside / "victim.txt", "delete"),
        ("symlink_modify", workspace / "modules/escape.txt", "write"),
    ):
        try:
            if action == "write":
                path.write_text("changed\n")
            else:
                path.unlink()
            results[name] = "allowed"
        except OSError as exc:
            results[name] = "denied" if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS) else f"error:{exc.errno}"
    print("M3_PERMISSION_RESULTS=" + json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()

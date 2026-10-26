"""Connect this checkout's secret-scanning hook without replacing existing hooks."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


HOOKS_PATH = "modules/security"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class InstallError(Exception):
    pass


def git(root, *args, missing_ok=False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode and not (missing_ok and result.returncode == 1):
        # Do not echo config contents or arbitrary Git diagnostics with secrets.
        raise InstallError("Git command failed: " + " ".join(args))
    return result.stdout


def config_values(root, *scope):
    output = git(root, "config", *scope, "--null", "--get-all", "core.hooksPath", missing_ok=True)
    return output.decode().split("\0")[:-1]


def install(root=PROJECT_ROOT, *, check=False):
    root = Path(root).resolve()
    if not shutil.which("git"):
        raise InstallError("Git is required on PATH.")
    try:
        actual = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).rstrip("\n")).resolve()
    except InstallError as exc:
        raise InstallError("This project is not an accessible Git working tree. Initialize or clone it first.") from exc
    if actual != root:
        raise InstallError("The project directory must be the Git working-tree root.")
    hook = root / HOOKS_PATH / "pre-commit"
    if not hook.is_file() or not os.access(hook, os.X_OK):
        raise InstallError("modules/security/pre-commit must exist and be executable.")
    config = root / ".gitleaks.toml"
    try:
        with config.open("rb") as stream:
            tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise InstallError(".gitleaks.toml must be readable, valid TOML.") from exc
    executable = shutil.which("gitleaks")
    if not executable:
        raise InstallError("Gitleaks is required on PATH; validated version: v8.30.1.")
    version = subprocess.run([executable, "version"], capture_output=True, timeout=10)
    if version.returncode:
        raise InstallError("Gitleaks could not run successfully.")

    values = config_values(root)
    if any(value != HOOKS_PATH for value in values):
        raise InstallError("Existing core.hooksPath differs. Resolve the hook configuration before installing; nothing changed.")
    if not values:
        hooks = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "hooks")).rstrip("\n"))
        if not hooks.is_absolute():
            hooks = root / hooks
        if hooks.exists() and any(not p.name.endswith(".sample") and (p.is_file() or p.is_symlink())
                                  for p in hooks.iterdir()):
            raise InstallError("Existing files in the default hooks directory. Review how to retain them before installing; nothing changed.")

    local = config_values(root, "--local")
    if local == [HOOKS_PATH]:
        return "Already installed: core.hooksPath=modules/security (repository local)."
    if check:
        return "Ready to install; no settings changed. Run without --check to connect the hook."
    git(root, "config", "--local", "core.hooksPath", HOOKS_PATH)
    if config_values(root, "--local") != [HOOKS_PATH] or config_values(root)[-1:] != [HOOKS_PATH]:
        raise InstallError("Hook configuration did not verify after installation; inspect Git configuration.")
    return "Installed: core.hooksPath=modules/security (repository local)."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check prerequisites and conflicts without changing settings")
    args = parser.parse_args(argv)
    try:
        print(install(check=args.check))
    except (InstallError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("Hook installation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

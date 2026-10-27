"""Install pinned native tools and check the harness's actual runtime contract.

Bootstrap supports Python 3.10; the installed controller requires 3.11 or 3.12.
dependencies.json stores the system and CLI dependency manifest.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class SetupError(Exception):
    pass


def version(text):
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        raise SetupError("Cannot parse version: " + text[:100])
    return tuple(int(x or 0) for x in match.groups())


def run(argv, *, env=None, cwd=None, expected=(0,), timeout=30):
    result = subprocess.run([str(x) for x in argv], env=env, cwd=cwd,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode not in expected:
        # Never print full scanner output, environment, or credential settings.
        raise SetupError(f"{Path(argv[0]).name} {argv[1:3]} exited {result.returncode}")
    return result


def load_manifest():
    spec = json.loads(Path(__file__).with_name("dependencies.json").read_text())
    if spec.get("format") != "loop-harness-system-requirements-v1":
        raise SetupError("Unsupported dependencies.json format")
    if set(spec["tools"]) != {"uv", "gitleaks", "codex", "claude"}:
        raise SetupError("Incomplete native dependency manifest")
    for name, item in spec["tools"].items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", item["version"]):
            raise SetupError(f"Unpinned version: {name}")
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise SetupError(f"Missing SHA256: {name}")
        if not item["url"].startswith(("https://github.com/", "https://downloads.claude.ai/")):
            raise SetupError(f"Non-official download URL: {name}")
    return spec


def check_platform(spec):
    required = spec["platform"]
    if (platform.system(), platform.machine()) != (required["system"], required["machine"]):
        raise SetupError("Only Linux x86_64 is supported by the current verification runtime")
    libc, release = platform.libc_ver()
    if libc != "glibc" or version(release) < version(required["glibc_minimum"]):
        raise SetupError("glibc >= " + required["glibc_minimum"] + " required")


def python_supported(text, spec):
    return version(spec["python"]["minimum"]) <= version(text) < version(spec["python"]["maximum_exclusive"])


def executable(name, env):
    path = shutil.which(name, path=env["PATH"])
    if not path:
        raise SetupError(f"Missing executable: {name}")
    return path


def check_versions(spec, env):
    rows = []
    for name, item in {**spec["system"], **{k: v for k, v in spec["tools"].items() if k != "uv"}}.items():
        path = executable(name, env)
        output = run([path, *item["version_args"]], env=env).stdout.strip()
        actual = version(output)
        if "minimum" in item:
            if actual < version(item["minimum"]):
                raise SetupError(f"{name}: requires >= {item['minimum']}; got {actual}")
        elif actual != version(item["version"]):
            raise SetupError(f"{name}: expected {item['version']}; got {actual}. Revalidate before changing the pin.")
        rows.append({"tool": name, "version": ".".join(map(str, actual)), "path": path})
    py = executable("python3", env)
    output = run([py, "--version"], env=env).stdout
    if not python_supported(output, spec):
        raise SetupError("Controller Python must be >=3.11,<3.13 (tomllib required); got " + output.strip())
    run([py, "-B", "-c", "import tomllib, ssl, sqlite3, fcntl; import modules.harness.run_loop; import modules.security.install_hooks"],
        env=env, cwd=ROOT)
    rows.append({"tool": "python3", "version": output.strip(), "path": py})
    return rows


def scan_probe(env):
    """Require both clean acceptance and real detection, including staged Git input."""
    env = {k: v for k, v in env.items() if not k.startswith(("GIT_", "GITLEAKS_"))}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    with tempfile.TemporaryDirectory(prefix="harness-install-scan-") as tmp:
        root = Path(tmp)
        sample = root / "sample.txt"
        sample.write_text("synthetic clean content\n")
        scanner = [executable("gitleaks", env)]
        flags = ["--config", str(ROOT / ".gitleaks.toml"), "--redact", "--no-banner", "--exit-code", "42"]
        run(scanner + ["dir", *flags, str(root)], env=env)
        git = executable("git", env)
        run([git, "init", "--quiet", "--template=", str(root)], env=env)
        run([git, "-C", str(root), "add", "sample.txt"], env=env)
        staged = scanner + ["git", "--pre-commit", "--staged", *flags, str(root)]
        run(staged, env=env)
        # A synthetic, never-issued token; do not log its value.
        sample.write_text('synthetic_token = "' + "ghp_" + uuid.uuid4().hex + 'A1b2"\n')
        run(scanner + ["dir", *flags, str(root)], env=env, expected=(42,))
        run([git, "-C", str(root), "add", "sample.txt"], env=env)
        run(staged, env=env, expected=(42,))


def check(spec, env):
    env = dict(env, DISABLE_AUTOUPDATER="1", PYTHONDONTWRITEBYTECODE="1")
    check_platform(spec)
    rows = check_versions(spec, env)
    from modules.harness.adapters import claude, codex
    from modules.harness.run_agent_once import preflight
    from modules.harness.process_runner import run_process
    from modules.harness.verification import VerificationRunner, process_ok
    from modules.harness.execution_budget import ExecutionBudget

    def runner(argv, **kwargs):
        kwargs.setdefault("env", env)
        return run_process(argv, **kwargs)

    for name, adapter in (("codex", codex), ("claude", claude)):
        preflight(adapter, executable(name, env), ROOT, runner)
    scan_probe(env)
    # Use the exact harness probe, including network namespaces and read-only mounts.
    # It finds bwrap through this process's PATH; checks run in an activated child.
    probe = VerificationRunner(ROOT).probe(ExecutionBudget(20))
    if not process_ok(probe):
        raise SetupError("bubblewrap isolation probe failed: " + probe.stderr.decode("utf-8", "replace")[:500])
    return {"status": "compatible", "versions": rows,
            "checks": ["CLI required flags", "Gitleaks clean/secret directory and staged Git", "harness bubblewrap isolation"],
            "not_checked": ["CLI authentication", "model access", "actual model execution", "all other tool versions"]}


def download_binary(item, target):
    """Verify pinned archive bytes; extract only the named regular member, never extractall."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=target.parent) as tmp:
        archive = Path(tmp) / "artifact"
        digest = hashlib.sha256()
        request = urllib.request.Request(item["url"], headers={"User-Agent": "loop-harness-installer"})
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise SetupError("SHA256 mismatch for " + target.name)
        binary = Path(tmp) / "binary"
        if item["member"]:
            with tarfile.open(archive, "r:gz") as tar:
                member = tar.getmember(item["member"])
                if not member.isfile():
                    raise SetupError("Archive executable is not a regular file")
                with tar.extractfile(member) as src, binary.open("wb") as out:
                    shutil.copyfileobj(src, out)
        else:
            archive.rename(binary)
        binary.chmod(0o755)
        output = run([binary, *item["version_args"]]).stdout
        if version(output) != version(item["version"]):
            raise SetupError("Downloaded executable reports the wrong version")
        binary.replace(target)


def install_system(spec):
    if not shutil.which("apt-get") or not shutil.which("dpkg-query"):
        raise SetupError("Automatic system installation supports Debian/Ubuntu apt. Install system requirements manually, then use --check.")
    missing = []
    for package in spec["apt_packages"]:
        result = run(["dpkg-query", "-W", "-f=${Status}", package], expected=(0, 1))
        if result.returncode or result.stdout != "install ok installed":
            missing.append(package)
    if missing:
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        if prefix and not shutil.which("sudo"):
            raise SetupError("Missing system packages require root or sudo: " + ", ".join(missing))
        # Keep apt/sudo interactive output visible, and propagate any failure.
        subprocess.run(prefix + ["apt-get", "update"], check=True)
        subprocess.run(prefix + ["apt-get", "install", "-y", "--no-install-recommends", *missing], check=True)
    for name, item in spec["system"].items():
        output = run([name, *item["version_args"]]).stdout
        if version(output) < version(item["minimum"]):
            raise SetupError(f"System {name} is too old; upgrade the OS/package to >= {item['minimum']}")


def activated(prefix):
    env = dict(os.environ)
    env.update(PATH=str(prefix / "bin") + os.pathsep + env.get("PATH", ""),
               DISABLE_AUTOUPDATER="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def python_install_env(prefix):
    # Caller UV_* overrides must not replace the pinned download catalog/mirror
    # or redirect managed installations outside the requested prefix.
    env = {k: v for k, v in activated(prefix).items() if not k.startswith("UV_")}
    env.update(UV_PYTHON_INSTALL_DIR=str(prefix / "python"), UV_CACHE_DIR=str(prefix / "cache"),
               UV_PYTHON_BIN_DIR=str(prefix / "python-bin"))
    return env


def install(spec, prefix):
    check_platform(spec)
    install_system(spec)
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # A lock prevents two installers from mutating the same prefix concurrently.
    import fcntl
    with (prefix / ".install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError("Another installer holds this prefix") from exc
        for name, item in spec["tools"].items():
            target = bin_dir / name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or version(run([target, *item["version_args"]]).stdout) != version(item["version"]):
                    raise SetupError(f"Conflicting {target}; use a new --prefix")
                print(f"Reusing {name} {item['version']}", flush=True)
            else:
                print(f"Installing {name} {item['version']}", flush=True)
                download_binary(item, target)
        env = python_install_env(prefix)
        py = bin_dir / "python3"
        if not py.exists():
            # uv verifies its embedded python-build-standalone download checksum.
            uv = str(bin_dir / "uv")
            pin = spec["python"]["install_version"]
            print(f"Installing CPython {pin}", flush=True)
            run([uv, "--no-config", "python", "install", pin], env=env, timeout=600)
            found = run([uv, "--no-config", "python", "find", "--managed-python", pin], env=env).stdout.strip()
            resolved = Path(found).resolve(strict=True)
            if not resolved.is_relative_to((prefix / "python").resolve()):
                raise SetupError("uv selected a Python outside the installation prefix")
            py.symlink_to(resolved)
        if version(run([py, "--version"], env=env).stdout) != version(spec["python"]["install_version"]):
            raise SetupError("Conflicting Python in prefix; use a new --prefix")
        result = subprocess.run([str(py), "-B", str(Path(__file__).resolve()), "--check"], env=env)
        if result.returncode:
            raise SetupError("Installed files retained, but compatibility checks failed. No successful installation reported.")
        script = ('# Source this file in each shell that runs the harness or its Git hooks.\n'
                  'export PATH=' + shlex.quote(str(bin_dir)) + ':"$PATH"\n'
                  'export DISABLE_AUTOUPDATER=1\n')
        (prefix / "env.sh").write_text(script)
        print("Installation verified. Activate: source " + shlex.quote(str(prefix / "env.sh")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check current PATH; no downloads/settings changes; temporary probes are removed")
    mode.add_argument("--dry-run", action="store_true", help="show manifest and destination without installing or probing")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/loop-harness/deps")
    args = parser.parse_args(argv)
    try:
        spec = load_manifest()
        prefix = args.prefix.expanduser().resolve()
        if args.dry_run:
            check_platform(spec)
            print(json.dumps({"prefix": str(prefix), "requirements": spec}, indent=2))
        elif args.check:
            print(json.dumps(check(spec, dict(os.environ)), indent=2))
        else:
            install(spec, prefix)
    except Exception as exc:
        print("Dependency setup failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

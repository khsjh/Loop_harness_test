"""Explicit stdlib-only system Python runtime for isolated Linux integration tests.

This prepares a reviewable mount list. Production verification never discovers
dependencies or executes ldd on model-provided binaries.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess


@lru_cache(maxsize=1)
def python_runtime():
    executable = str(Path("/usr/bin/python3").resolve(strict=True))
    metadata = json.loads(subprocess.check_output([
        executable, "-I", "-S", "-c",
        "import json,sysconfig; print(json.dumps(sysconfig.get_paths()))"], text=True))
    stdlib = Path(metadata["stdlib"])
    binaries = [executable, *(str(p) for p in sorted((stdlib / "lib-dynload").glob("*.so")))]
    linked = subprocess.check_output(["/usr/bin/ldd", *binaries], text=True)
    if "not found" in linked:
        raise RuntimeError("system Python has an unresolved runtime dependency")
    libraries = re.findall(r"(?:=>\s+|^\s*)(/\S+)\s+\(", linked, re.MULTILINE)
    paths = sorted({executable, str(stdlib), *libraries})
    return executable, tuple(paths)

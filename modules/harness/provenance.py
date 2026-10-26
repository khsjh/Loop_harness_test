"""Identify the local harness source at execution time, without importing tests."""
from .recording import HARNESS_ROOT
from .run_store import digest


def source_manifest():
    files = sorted((HARNESS_ROOT / "modules/harness").rglob("*.py"))
    files.append(HARNESS_ROOT / "modules/save_dialog.py")
    return {str(p.relative_to(HARNESS_ROOT)): digest(p.read_bytes()) for p in files}

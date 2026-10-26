"""Read-only fixture inventory. No deletion or retention-policy migration."""
import json
from pathlib import Path

from .contracts import loads
from .recording import HARNESS_ROOT


def inventory(root=HARNESS_ROOT):
    root = Path(root).resolve()
    docs = [(str(p.relative_to(root)), p.read_text()) for p in (root / "docs").rglob("*.md")]
    manifests = {}
    for path in (root / "runs").glob("*/fixture-manifest.json"):
        data = loads(path.read_bytes())
        manifests.setdefault(data["path"], []).append((path, data))
    results = []
    for path in sorted((root / "modules/tests/fixtures").iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        references = [name for name, text in docs if path.name in text]
        records = manifests.get(str(path.resolve()), [])
        reasons = ["document_reference"] if references else []
        if not records:
            reasons.append("unknown_legacy_fixture")
        for manifest, data in records:
            if data.get("retention") != "candidate":
                reasons.append("keep_policy")
            try:
                from .run_store import RunStore
                state = RunStore(root, run_id=manifest.parent.name).state
                if state["status"] != "success" or state["recovery_required"]:
                    reasons.append("active_or_unresolved_run")
            except (OSError, ValueError, KeyError):
                reasons.append("unverified_run")
            if any(manifest.parent.name in text for _, text in docs):
                reasons.append("run_document_reference")
        size = sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
        results.append({"path": str(path), "bytes": size, "references": references,
                        "candidate": not reasons, "keep_reasons": sorted(set(reasons))})
    return results


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))

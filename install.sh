#!/usr/bin/env bash
# Install/check the system manifest, not Python packages from PyPI.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Bootstrap requires python3 (3.10+). On Ubuntu/Debian: sudo apt-get install python3 ca-certificates' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo 'Bootstrap Python 3.10+ is required; controller Python is installed separately.' >&2
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$root/modules/setup/dependencies.py" "$@"

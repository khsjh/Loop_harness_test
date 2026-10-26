"""Controller process used only for SIGKILL recovery regression tests."""
from pathlib import Path
import sys

from modules.harness.contracts import loads
from modules.harness.run_loop import execute
from modules.tests.test_m5 import OfflineAgent, OfflineVerifier

root, request_path, approval_path = map(Path, sys.argv[1:])
execute(loads(request_path.read_bytes()), approval_path, root=root,
        agent_call=OfflineAgent("hang"), verifier_factory=OfflineVerifier)

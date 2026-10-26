"""Offline process-lifecycle fixture; unreachable through production CLI options."""
import sys
from unittest.mock import patch

from modules.tests import background_m6_check as background
from modules.tests.test_m5 import OfflineAgent, OfflineVerifier
from modules.harness.run_loop import execute


def offline(request, approval, **kwargs):
    return execute(request, approval, agent_call=OfflineAgent("hang"), verifier_factory=OfflineVerifier, **kwargs)


with patch.object(background, "execute", offline):
    sys.exit(background.main(sys.argv[1:]))

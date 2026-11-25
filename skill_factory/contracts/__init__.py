"""Mount 02_l1_l4_extractor and workspace/contracts under skill_factory.contracts."""

from pathlib import Path
from .. import DEPLOY_ROOT, WORKSPACE_ROOT

__path__ = [
    str(DEPLOY_ROOT / "02_l1_l4_extractor"),
    str(WORKSPACE_ROOT / "contracts"),
]

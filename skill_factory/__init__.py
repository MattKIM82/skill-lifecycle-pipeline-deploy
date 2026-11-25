"""Bootstrap package linking separated deploy folders (02_l1_l4_extractor, 03_l5_l8_pipeline, 04_console, workspace)."""

from __future__ import annotations

import os
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(os.environ.get("SKILL_FACTORY_WORKSPACE", DEPLOY_ROOT / "workspace")).resolve()

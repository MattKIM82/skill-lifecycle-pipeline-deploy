#!/usr/bin/env python3
"""Standalone CLI for L5-L8 Skill Lifecycle Engineering Pipeline (03_l5_l8_pipeline)."""

from __future__ import annotations

import sys
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
if str(DEPLOY_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT))

from skill_factory.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

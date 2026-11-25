#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${1:-8790}"

if [ -x "${DEPLOY_ROOT}/../.venv/bin/python" ]; then
  PY="${DEPLOY_ROOT}/../.venv/bin/python"
elif [ -x "${DEPLOY_ROOT}/.venv/bin/python" ]; then
  PY="${DEPLOY_ROOT}/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

export PYTHONPATH="${DEPLOY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
echo "Starting Skill Factory Console (deploy root: ${DEPLOY_ROOT}, workspace: ${SKILL_FACTORY_WORKSPACE:-${DEPLOY_ROOT}/workspace}) on port ${PORT}..."
exec "${PY}" -m skill_factory.cli dashboard --port "${PORT}"

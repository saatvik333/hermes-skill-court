#!/usr/bin/env bash
# Skill Court verdict-quality benchmark: the plugin's tests must pass, then its real trial
# scheduler and scoring judge 3000 simulated skills with known true effects (fixed seed, offline).
set -euo pipefail
cd "$(dirname "$0")"
find . -name __pycache__ -prune -exec rm -rf {} +
uv run --quiet --no-project --with pytest python -m pytest -q -x -p no:cacheprovider >/dev/null
uv run --quiet --no-project python bench/simulate.py

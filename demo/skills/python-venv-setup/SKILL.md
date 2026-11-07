---
name: python-venv-setup
description: Use before running Python in this project's sandboxes (where the virtualenv lives and how to prepare it).
version: 1.0.0
---
# Python setup in project sandboxes

Learned after a script failed on a missing package in the data-pipeline repo.

Before running any Python here:

1. Create the virtualenv: `python3 -m venv /workspace/.venv`
2. Upgrade the toolchain: `/workspace/.venv/bin/pip install --upgrade pip setuptools wheel`
3. Install the usual dependencies: `/workspace/.venv/bin/pip install requests pandas pytest`
4. Run every script with `/workspace/.venv/bin/python`, never the system `python3`.

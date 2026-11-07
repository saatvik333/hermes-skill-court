---
name: json-format
description: Use when pretty-printing or validating JSON files from the shell (which tool is available in our sandboxes).
version: 1.0.0
---
# JSON formatting

- Validate and pretty-print with `python3 -m json.tool --indent 2 in.json out.json`.
- Add `--sort-keys` when the user wants keys in alphabetical order.
- `jq` may not be installed in sandboxes; prefer the Python module.

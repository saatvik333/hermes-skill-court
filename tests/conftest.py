"""Expose the plugin directory as package ``skill_court`` without running its ``__init__``.

``register(ctx)`` imports Hermes modules; the pure modules under test do not, so they import
standalone through this package shell.
"""

import sys
import types
from pathlib import Path

_package = types.ModuleType("skill_court")
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "skill-court")]
sys.modules.setdefault("skill_court", _package)

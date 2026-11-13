"""The generated tool table in docs/API.md must match the registry exactly
— the guard against the four-contradictory-tool-counts failure mode."""

import importlib.util
import subprocess
import sys
from pathlib import Path

V5_ROOT = Path(__file__).resolve().parents[1]


def test_tool_table_matches_registry():
    proc = subprocess.run(
        [sys.executable, str(V5_ROOT / "scripts" / "dump_tool_table.py"),
         "--check"],
        capture_output=True, text=True, cwd=str(V5_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_version_is_45_line():
    spec = importlib.util.spec_from_file_location(
        "_v5_init", V5_ROOT / "ewsmcp" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.__version__ == "4.5.0a2"
    pyproject = (V5_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{mod.__version__}"' in pyproject

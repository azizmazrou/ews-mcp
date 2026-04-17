"""Portable entrypoint wrapper for the MCP server.

Some MCP clients (notably Claude Desktop MSIX on Windows) launch Python with
an unrelated working directory. This wrapper pins CWD and sys.path to the
directory this file lives in so ``python run_server.py`` works from anywhere.
"""

import os
import runpy
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

os.chdir(_PROJECT_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

runpy.run_module("src.main", run_name="__main__")

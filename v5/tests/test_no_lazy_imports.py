"""AST sentinel: no ``exchangelib`` imports inside function bodies in v5.

Lifted from the v3 sentinel (lazy ``OofReply`` and ``FileAttachment``
imports failed only at CALL time after an exchangelib upgrade, because
unit tests patched the same lazy path).
v5 policy is stricter: NO exemptions — every exchangelib import lives at
module top where a removed symbol fails at import/collection time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1] / "ewsmcp"


def _function_scope_imports(tree: ast.AST):
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def qualname(node):
        names: list[str] = []
        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.insert(0, cur.name)
            cur = parents.get(id(cur))
        return ".".join(names)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            inside = qualname(node)
            enclosing = parents.get(id(node))
            # only report imports that are NOT at module level
            if inside and not isinstance(enclosing, ast.Module):
                yield node.lineno, inside, node


def test_no_lazy_exchangelib_imports_in_v5():
    offenders: list[str] = []
    for path in PKG_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(PKG_ROOT.parent).as_posix()
        for lineno, qual, node in _function_scope_imports(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            else:
                mod = node.names[0].name if node.names else ""
            if mod.startswith("exchangelib"):
                offenders.append(f"{rel}:{lineno} (in {qual}) — {ast.unparse(node)}")
    if offenders:
        pytest.fail(
            "Lazy exchangelib imports inside function bodies (NO exemptions "
            "in v5 — hoist to module top):\n  " + "\n  ".join(offenders)
        )

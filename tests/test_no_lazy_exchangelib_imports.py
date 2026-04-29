"""Sentinel test: no lazy ``from exchangelib import ...`` inside function bodies.

History: lazy imports of ``OofReply`` (commit 62cfb26) and ``FileAttachment``
(commit 0fb0948) inside execute methods caused two production outages —
when the imported symbol disappeared in exchangelib 5.x, the import only
failed at *call time*, never at *load time* or *test time* (because the
unit tests patched the same lazy path). Hoisting these imports to module
top makes the failure happen at import / pytest collection — the loudest,
earliest, cheapest place to catch it.

Module-level ``try: from exchangelib... except: ...`` guarded imports are
allowed (they fail loudly at import if the *try* branch breaks, and the
fallback is a deliberate compatibility shim).

If you have a *legitimate* reason to lazy-import inside a function body,
add the (file, function-qualified-name) pair to ``_ALLOWED`` with a
one-line justification. Each entry is technical debt.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# Set of (relative_path, function_qualified_name) explicitly allowed to
# lazy-import. Keep small. Each is technical debt.
_ALLOWED_EXCHANGELIB: set[tuple[str, str]] = set()

_ALLOWED_INTER_TOOL: set[tuple[str, str]] = {
    # oof_policy_tools.ApplyOofPolicyTool.execute imports ForwardEmailTool /
    # CreateForwardDraftTool — circular via tools/__init__ registration.
    ("src/tools/oof_policy_tools.py", "ApplyOofPolicyTool.execute"),
    # meeting_prep_tools imports ReadAttachmentTool — circular via
    # tools/__init__ registration order.
    ("src/tools/meeting_prep_tools.py", "PrepareMeetingTool._collect_attachment_previews"),
}


def _iter_python_files(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _function_scope_imports(tree: ast.AST):
    """Yield (lineno, qualname, node) for every Import / ImportFrom that
    sits inside a FunctionDef or AsyncFunctionDef body (at any depth)."""
    # Build parent map.
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def enclosing_function(node):
        cur = parents.get(id(node))
        names: list[str] = []
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.insert(0, cur.name)
            elif isinstance(cur, ast.ClassDef):
                names.insert(0, cur.name)
            cur = parents.get(id(cur))
        return ".".join(names) if names else None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            qual = enclosing_function(node)
            if qual:  # only function/method-scope imports
                yield node.lineno, qual, node


def test_no_lazy_exchangelib_imports_in_src():
    """Hoist every ``from exchangelib import ...`` to module top.

    Each function-body import is one place where a removed symbol slips
    past the test suite (mocks accept any name) and detonates in prod."""
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for lineno, qual, node in _function_scope_imports(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            else:  # ast.Import
                mod = node.names[0].name if node.names else ""
            if not mod.startswith("exchangelib"):
                continue
            if (rel, qual) in _ALLOWED_EXCHANGELIB:
                continue
            offenders.append(f"{rel}:{lineno} (in {qual}) — {ast.unparse(node)}")
    if offenders:
        pytest.fail(
            "Lazy `from exchangelib` imports inside function bodies. Hoist "
            "to module top — each is a patch-path drift hazard (see the "
            "OofReply / FileAttachment outages).\n  " + "\n  ".join(offenders)
        )


def test_no_lazy_inter_tool_imports_in_src_tools():
    """Lazy ``from .other_tool_module import X`` is the same hazard with a
    different blast radius. The rule_tools.py:165 outage was exactly this:
    wrong module name in the lazy `from .folder_tools import` — the
    ImportError fired only inside the per-action try/except, so the rule
    silently never moved any messages."""
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        if "/tools/" not in rel and "\\tools\\" not in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for lineno, qual, node in _function_scope_imports(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            # Catch BOTH `from .x import y` (level=1) AND `from ..x import y`.
            if node.level == 0:
                continue
            if (rel, qual) in _ALLOWED_INTER_TOOL:
                continue
            # Don't flag intra-package memory/utils imports — those are
            # generally fine; we care specifically about cross-tool wiring.
            if mod and not mod.startswith(("memory", "exceptions", "config",
                                            "auth", "utils", "ai")):
                offenders.append(
                    f"{rel}:{lineno} (in {qual}) — {ast.unparse(node)}"
                )
    if offenders:
        pytest.fail(
            "Lazy intra-package imports inside function bodies in "
            "src/tools/. Each is a wrong-module / wrong-kwarg / missing-await "
            "hazard (see rule_tools.py:165 history). Hoist or add to "
            "_ALLOWED_INTER_TOOL with justification.\n  "
            + "\n  ".join(offenders)
        )

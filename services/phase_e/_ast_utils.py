"""Shared AST helper for the E8/E13 authority-scan tooling.

Kept in its own tiny module (rather than duplicated in ``authority_scan.py``
and ``json_compat.py``) so both scanners derive a call site's *stable*
identity — the enclosing function/method qualname — the same way.
"""

from __future__ import annotations

import ast


def qualname_at(tree: ast.AST, lineno: int) -> str:
    """Dotted qualname (``Class.method`` / ``function``) enclosing ``lineno``.

    Deliberately NOT line-number-based as an identity: unrelated edits that
    shift line numbers elsewhere in the file must not change the identity of
    an already-reviewed call site (H5 audit finding — a line-number-keyed
    allow-list can't distinguish cosmetic drift from a real new site).
    """
    best = ["<module>"]

    def visit(node: ast.AST, path: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            new_path = path
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_path = path + [child.name]
            start = getattr(child, "lineno", None)
            end = getattr(child, "end_lineno", None)
            if start is not None and end is not None and start <= lineno <= end:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    best[0] = ".".join(new_path)
            visit(child, new_path)

    visit(tree, [])
    return best[0]

#!/usr/bin/env python3
"""Batch 147 — static check for references to model columns that do not exist.

WHY THIS EXISTS

  type object 'Ingredient' has no attribute 'inventory_code'

500'd every kitchen section order page, and every gate we run passed clean
beforehand: the file compiles, the module imports, the template parses. A
SQLAlchemy declarative model only raises on ATTRIBUTE ACCESS, so a wrong column
name sits silently in the source until a user opens the one page that touches
it. `Ingredient.ingredient_code` and `RecipeIngredient.inventory_code` are two
different names for the same idea, three lines apart in the same function —
exactly the kind of thing a human review slides over.

WHAT IT DOES

Walks every .py file, resolves `from app.models.x import Y [as Z]`, then checks
every `Z.attr` reference against the real class. Reports anything that is not a
mapped column, relationship, or ordinary class attribute.

Read-only. No database needed. Run it as part of the batch gate:

    python scripts/model_attr_audit.py

Exit code 1 if anything is unresolved, so it can gate a commit.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SKIP_DIRS = {"venv", ".git", "__pycache__", "node_modules", ".venv"}

# Attributes every SQLAlchemy class carries that are not declared as columns.
SA_BUILTINS = {
    "metadata", "registry", "query", "__table__", "__tablename__",
    "__mapper__", "_sa_instance_state", "c",
}


def iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def load_model(module_path: str, class_name: str):
    try:
        mod = importlib.import_module(module_path)
    except Exception:
        return None
    return getattr(mod, class_name, None)


def audit_file(path: str) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return []

    # local alias -> model class
    aliases: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "models" in node.module:
            mod = node.module
            if node.level:                      # relative import inside app/
                mod = "app.models." + mod.split(".")[-1]
            for a in node.names:
                cls = load_model(mod, a.name)
                if cls is not None and hasattr(cls, "__tablename__"):
                    aliases[a.asname or a.name] = cls

    problems: list[tuple[int, str, str]] = []
    if not aliases:
        return problems

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        cls = aliases.get(node.value.id)
        if cls is None:
            continue
        attr = node.attr
        if attr in SA_BUILTINS or attr.startswith("__"):
            continue
        if not hasattr(cls, attr):
            problems.append((node.lineno, f"{node.value.id}.{attr}", cls.__name__))
    return problems


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    total = 0
    scanned = 0
    for path in iter_py_files(os.path.join(root, "app")):
        scanned += 1
        for lineno, ref, cls_name in audit_file(path):
            rel = os.path.relpath(path, root)
            print(f"  {rel}:{lineno}  {ref}  -> {cls_name} has no such attribute")
            total += 1
    print(f"\nScanned {scanned} file(s). Unresolved model attributes: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())

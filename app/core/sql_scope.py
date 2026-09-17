# app/core/sql_scope.py

from __future__ import annotations

import re

SCOPED_TABLES = {
    "customer_orders", "order_lines", "bom_lines", "store_issuance_lines",
    "kitchen_section_transactions", "qc_checks", "packing_dispatch",
}

_TAIL = re.compile(r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|WINDOW)\b", re.I)
_FROM = re.compile(r"\bFROM\s+([A-Za-z_][\w]*)\s*(?:AS\s+)?([A-Za-z_]\w*)?", re.I)
_KEYWORD = {"where", "group", "order", "limit", "having", "join", "inner", "left",
            "right", "outer", "cross", "on", "union", "using", "straight_join"}


def _top_level_spans(sql: str) -> list[tuple[int, int]]:
    """Character ranges of `sql` that sit at parenthesis depth 0."""
    spans, depth, start = [], 0, 0
    for i, ch in enumerate(sql):
        if ch == "(":
            if depth == 0:
                spans.append((start, i))
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                start = i + 1
            if depth < 0:                      # unbalanced — refuse
                return []
    if depth != 0:
        return []
    spans.append((start, len(sql)))
    return spans


def _at_top_level(sql: str, pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def scope_sql(sql: str, param: str = "scope_cid") -> tuple[str, bool]:
    """Return (sql_with_company_condition, changed).

    `changed` is False when the query needs no scoping or could not be scoped
    safely — the caller can then decide whether to bind the parameter.
    """
    if not sql or ":" + param in sql:
        return sql, False
    spans = _top_level_spans(sql)
    if not spans:
        return sql, False
    if any(re.search(r"\bUNION\b", sql[a:b], re.I) for a, b in spans):
        return sql, False

    froms = [m for m in _FROM.finditer(sql) if _at_top_level(sql, m.start(), spans)]
    if len(froms) != 1:
        return sql, False
    table = froms[0].group(1).lower()
    if table not in SCOPED_TABLES:
        return sql, False
    alias = froms[0].group(2)
    if alias and alias.lower() in _KEYWORD:
        alias = None

    scan_from = froms[0].end(1)
    prefix = f"{alias}." if alias else f"{table}."
    clause = f"({prefix}company_id = :{param} OR {prefix}company_id IS NULL)"

    wheres = [m for m in re.finditer(r"\bWHERE\b", sql, re.I)
              if _at_top_level(sql, m.start(), spans)]
    if len(wheres) > 1:
        return sql, False

    if wheres:
        # Append to the existing top-level WHERE, before its GROUP/ORDER/LIMIT.
        after = wheres[0].end()
        tail = next((m for m in _TAIL.finditer(sql)
                     if m.start() > after and _at_top_level(sql, m.start(), spans)), None)
        cut = tail.start() if tail else len(sql)
        return f"{sql[:cut].rstrip()} AND {clause} {sql[cut:]}".rstrip(), True

    # No WHERE at all — insert one before the first top-level tail keyword.
    tail = next((m for m in _TAIL.finditer(sql)
                 if m.start() >= scan_from and _at_top_level(sql, m.start(), spans)), None)
    cut = tail.start() if tail else len(sql)
    return f"{sql[:cut].rstrip()} WHERE {clause} {sql[cut:]}".rstrip(), True


def scoped(sql: str, params: dict | None = None, cid: int = 1,
           param: str = "scope_cid") -> tuple[str, dict]:
    """Convenience wrapper: returns the SQL and the params dict to execute with."""
    out, changed = scope_sql(sql, param)
    merged = dict(params or {})
    # Bind when we added the clause OR when the query already carries the
    # placeholder (a hand-scoped UNION, for example) — otherwise that query
    # would fail with "parameter not bound".
    if changed or f":{param}" in out:
        merged[param] = cid
    return out, merged


def audit_unscoped(sql: str) -> bool:
    """True when this query reads a scoped table but could not be scoped —
    used by scripts/test_company_scope.py so a refusal is visible, not silent."""
    if ":scope_cid" in sql or "company_id" in sql:
        return False
    _, changed = scope_sql(sql)
    if changed:
        return False
    return any(re.search(r"\bFROM\s+" + t + r"\b", sql, re.I) for t in SCOPED_TABLES)

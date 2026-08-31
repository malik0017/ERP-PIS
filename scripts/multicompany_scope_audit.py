#!/usr/bin/env python3
"""Batch 154 — Multi-company raw-SQL scope audit.

Scans every text(\"\"\"...\"\"\") SQL block in app/modules for queries that touch a
COMPANY-SCOPED table (one that has a company_id column) but carry NO company_id
predicate. Splits results into:

  HIGH RISK   — list/aggregate reads or bulk UPDATEs with neither a company_id
                filter nor a globally-unique key (order_no/username/id/...).
                These can leak or cross-write data once a 2nd company exists.
  LOWER RISK  — no company_id, but filtered by a unique key, so scoped in practice.

Run from the project root:   python scripts/multicompany_scope_audit.py
Exit code is non-zero when HIGH-RISK findings exist, so it can gate CI.
"""
import re, pathlib, sys

SCOPED = {"bom_lines","brands","chefs","customer_orders","customers","head_chef_plans",
    "kitchen_locations","kitchen_section_transactions","kitchen_sections","master_records",
    "order_lines","packing_dispatch","qc_checks","recipe_ingredients","recipes",
    "revenue_streams","store_issuance_lines","suppliers","system_settings","users"}
UNIQUE_KEYS = ("order_no","username","email","= :i","id = :","id=:","dispatch_no",
               "recipe_code","recipe_no","customer_code")

def audit(root="app/modules"):
    root = pathlib.Path(root)
    block_re = re.compile(r'text\(\s*(?:f?"""|f?\'\'\')(.*?)(?:"""|\'\'\')', re.S)
    high, low = [], []
    for f in sorted(root.rglob("*.py")):
        src = f.read_text(errors="ignore")
        for m in block_re.finditer(src):
            sql = m.group(1).lower()
            if "company_id" in sql:
                continue
            tabs = [t for t in SCOPED if re.search(r'\b(from|join|into|update)\s+'+re.escape(t)+r'\b', sql)]
            if not tabs:
                continue
            kind = sql.split()[0] if sql.split() else "?"
            if kind in ("alter", "create", "insert"):
                continue
            line = src[:m.start()].count("\n") + 1
            rec = (str(f), line, kind, tabs)
            (low if any(k in sql for k in UNIQUE_KEYS) else high).append(rec)
    return high, low

if __name__ == "__main__":
    high, low = audit()
    print(f"### HIGH RISK ({len(high)}) — no company_id AND no unique-key filter:")
    for f, l, k, t in high:
        print(f"  {f}:{l}  {k.upper():6} {t}")
    print(f"\n### LOWER RISK ({len(low)}) — no company_id but filtered by a unique key (scoped in practice).")
    sys.exit(1 if high else 0)

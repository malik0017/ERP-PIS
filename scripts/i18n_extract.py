"""ISFC PIMS — i18n string extractor (Batch 96).

THE PROBLEM
-----------
Templates call t('...') in 911 distinct places. app/i18n/ar.json has 213 keys.
Roughly 77% of the interface silently falls through to English when a user
switches to Arabic — the t() filter returns the key unchanged when there is no
translation, so nothing looks broken, it just stays English.

The RTL and t() infrastructure is correct. The dictionary is simply not
populated.

WHAT THIS DOES
--------------
Scans every template and Python module for translatable strings, then rewrites
app/i18n/en.json and app/i18n/ar.json so that:

  * every discovered string is present as a key
  * EXISTING Arabic translations are preserved exactly — this never
    overwrites human work
  * new strings are written with an empty value ("") so they are trivial to
    find and fill

Then it reports coverage.

WHY NOT MACHINE-TRANSLATE HERE
------------------------------
Deliberately not wired to any translation API. Kitchen and ERP terminology is
exactly where machine translation fails: "portion", "batch", "yield",
"issuance", "requisition", "trayline" all have specific operational meanings
your staff already use in Arabic. Auto-filling them produces text that looks
translated and reads wrong to the people who have to work in it every day.

The intended workflow:
    1. python scripts/i18n_extract.py            -> see the gap
    2. python scripts/i18n_extract.py --write    -> ar.json gains empty keys
    3. Optionally machine-translate the empty values as a FIRST DRAFT
    4. A native Arabic speaker who knows the operation reviews the file
    5. python scripts/i18n_extract.py            -> confirm 100%

Do NOT translate stored data. Store Arabic as Arabic — the database is
already utf8mb4. Translating user input on submit and storing English is
irreversible data loss (customer names, ingredient names, complaint text).

USAGE
    python scripts/i18n_extract.py                  # report only
    python scripts/i18n_extract.py --write          # update the json files
    python scripts/i18n_extract.py --missing-only   # list untranslated keys
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Batch 96: run from anywhere (same bootstrap as the other scripts).
# ---------------------------------------------------------------------------
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
_root = _here
for _ in range(4):
    if _os.path.isdir(_os.path.join(_root, "app")):
        break
    _parent = _os.path.dirname(_root)
    if _parent == _root:
        break
    _root = _parent
if not _os.path.isdir(_os.path.join(_root, "app")):
    _sys.stderr.write("ERROR: could not locate the project root (folder containing app/).\n")
    _sys.exit(2)
_os.chdir(_root)

import json
import re

TEMPLATE_DIR = _os.path.join("app", "templates")
I18N_DIR = _os.path.join("app", "i18n")

# t('...') / t("...") in templates, and t('...') in Python.
# Deliberately does not try to catch t(variable) — a runtime value can't be
# extracted statically, and pretending otherwise produces junk keys.
PATTERNS = [
    re.compile(r"""\bt\(\s*'((?:[^'\\]|\\.)*)'\s*\)"""),
    re.compile(r"""\bt\(\s*"((?:[^"\\]|\\.)*)"\s*\)"""),
    re.compile(r"""\|\s*t\b"""),  # counted separately, not extracted
]


def discover() -> tuple[set[str], dict[str, int]]:
    found: set[str] = set()
    per_file: dict[str, int] = {}

    for base in (TEMPLATE_DIR, _os.path.join("app", "modules"), _os.path.join("app", "core")):
        if not _os.path.isdir(base):
            continue
        for root, _dirs, files in _os.walk(base):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith((".html", ".py")):
                    continue
                path = _os.path.join(root, fn)
                try:
                    src = open(path, encoding="utf-8").read()
                except Exception:
                    continue
                hits = 0
                for pat in PATTERNS[:2]:
                    for m in pat.finditer(src):
                        raw = m.group(1)
                        # Unescape the quote style that was used.
                        val = raw.replace("\\'", "'").replace('\\"', '"')
                        val = val.strip()
                        if not val:
                            continue
                        # Skip strings that are obviously not UI copy.
                        if val.startswith(("/", "http", "{{", "{%")):
                            continue
                        found.add(val)
                        hits += 1
                if hits:
                    per_file[path] = hits
    return found, per_file


def load(name: str) -> dict:
    path = _os.path.join(I18N_DIR, name)
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def save(name: str, data: dict) -> None:
    path = _os.path.join(I18N_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def main() -> int:
    write = "--write" in _sys.argv
    missing_only = "--missing-only" in _sys.argv

    found, per_file = discover()
    en = load("en.json")
    ar = load("ar.json")

    # A key counts as translated when it has a non-empty Arabic value that is
    # not just the English string echoed back.
    def translated(k: str) -> bool:
        v = (ar.get(k) or "").strip()
        return bool(v) and v != k

    have = {k for k in found if translated(k)}
    missing = sorted(found - have)

    print("\n" + "=" * 66)
    print("i18n COVERAGE")
    print("=" * 66)
    print(f"  Files containing t() ......... {len(per_file)}")
    print(f"  Distinct strings discovered .. {len(found)}")
    print(f"  Keys currently in ar.json .... {len(ar)}")
    print(f"  Translated ................... {len(have)}")
    print(f"  UNTRANSLATED ................. {len(missing)}")
    pct = (len(have) / len(found) * 100) if found else 100.0
    print(f"  Arabic coverage .............. {pct:.1f}%")

    if missing_only:
        print("\n--- UNTRANSLATED ---")
        for k in missing:
            print("  " + k)

    if write:
        # Preserve every existing translation; only ADD keys.
        new_en = dict(en)
        new_ar = dict(ar)
        added = 0
        for k in sorted(found):
            if k not in new_en:
                new_en[k] = k
            if k not in new_ar:
                new_ar[k] = ""
                added += 1
        save("en.json", new_en)
        save("ar.json", new_ar)
        print(f"\n  Wrote {I18N_DIR}/en.json  ({len(new_en)} keys)")
        print(f"  Wrote {I18N_DIR}/ar.json  ({len(new_ar)} keys, {added} new empty)")
        print("  Existing Arabic translations were preserved untouched.")
    else:
        print("\n  (report only — re-run with --write to add the missing keys)")

    print("\n  Top files by translatable strings:")
    for path, n in sorted(per_file.items(), key=lambda x: -x[1])[:10]:
        print(f"    {n:4}  {path}")
    print()
    return 0


if __name__ == "__main__":
    _sys.exit(main())

#!/usr/bin/env python3
"""Batch 156 — i18n parity checker.

Lists keys present in en.json but MISSING in ar.json (untranslated), and keys in
ar.json with no en counterpart (ORPHAN / likely stale). Run from project root:

    python scripts/i18n_parity.py

Exit code is non-zero when any en key is missing from ar, so it can gate CI.
"""
import json, pathlib, sys

def load(p):
    return json.load(open(p, encoding="utf-8"))

def main():
    base = pathlib.Path("app/i18n")
    en = load(base / "en.json")
    ar = load(base / "ar.json")
    en_k, ar_k = set(en), set(ar)
    missing = sorted(en_k - ar_k)
    orphan = sorted(ar_k - en_k)
    print(f"en: {len(en_k)} keys | ar: {len(ar_k)} keys")
    print(f"\nMISSING in ar ({len(missing)}) — English strings with no Arabic:")
    for k in missing[:50]:
        print("   -", repr(k[:60]))
    if len(missing) > 50:
        print(f"   ... +{len(missing)-50} more")
    print(f"\nORPHAN in ar ({len(orphan)}) — Arabic keys not used in en (likely stale).")
    sys.exit(1 if missing else 0)

if __name__ == "__main__":
    main()

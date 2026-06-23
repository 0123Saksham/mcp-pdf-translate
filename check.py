#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check.py — pdf-translate skill, stage 3. Usage: python3 check.py <workdir>"""
import sys, os, glob, json
WORK = sys.argv[1] if len(sys.argv) > 1 else "work"
TT = os.path.join(WORK, "to_translate.json")
TR = os.path.join(WORK, "translations.json")
def die(msg): print("ERROR: "+msg, file=sys.stderr); sys.exit(4)
parts = sorted(glob.glob(os.path.join(WORK, "tr_part*.json")))
if parts:
    merged = {}
    for p in parts:
        try: merged.update(json.load(open(p, encoding="utf-8")))
        except Exception as e: die(f"{p} is not valid JSON: {e}")
    try: json.dump(merged, open(TR, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        tmp = TR+".tmp"
        json.dump(merged, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, TR)
    print(f"merged {len(parts)} chunk file(s) -> {TR}  ({len(merged)} keys)")
if not os.path.exists(TT): die(f"{TT} not found - run extract.py first.")
if not os.path.exists(TR): die(f'{TR} not found - write translations as {{"1":"..."}} or chunk files tr_part*.json.')
try: todo = json.load(open(TT, encoding="utf-8"))
except Exception as e: die(f"{TT} is not valid JSON: {e}")
try: T = json.load(open(TR, encoding="utf-8"))
except Exception as e: die(f"{TR} is not valid JSON ({e}). Must be ONE JSON object — no markdown fences.")
if not isinstance(T, dict): die(f"{TR} must be a JSON object, not {type(T).__name__}.")
need = {e["k"]: e for e in todo}
missing, empty, over, nl = [], [], [], []
for k, e in need.items():
    if k not in T: missing.append(k); continue
    v = str(T[k])
    if not v.strip(): empty.append(k); continue
    if len(v) > e["max"]: over.append((k, len(v), e["max"], e["s"][:30], v[:60]))
    if v.count("\n") != e["s"].count("\n"): nl.append((k, e["s"].count("\n"), v.count("\n")))
extra = [k for k in T if k not in need]
identical = [(k, str(T[k])) for k in need if k in T and str(T[k]).strip() == need[k]["s"].strip()]
print(f"required: {len(need)} | provided: {len(T)} | missing: {len(missing)} | "
      f"empty: {len(empty)} | over-max: {len(over)} | newline-mismatch: {len(nl)} | "
      f"extra: {len(extra)} | identical-to-source: {len(identical)}")
hard = False
if missing:
    hard = True
    print(f"\nMISSING ({len(missing)}) - add these keys to translations.json:")
    print("  " + ", ".join(missing[:40]) + (" ..." if len(missing) > 40 else ""))
if empty:
    hard = True
    print(f"\nEMPTY ({len(empty)}) - these keys have blank values:")
    print("  " + ", ".join(empty[:40]) + (" ..." if len(empty) > 40 else ""))
if nl:
    hard = True
    print(f"\nNEWLINE MISMATCH ({len(nl)}) - keep the SAME number of line breaks as source:")
    for k, a, b in nl[:40]: print(f"  {k}: source has {a}, yours has {b}")
if extra:
    print(f"\nNOTE - {len(extra)} extra key(s) (harmless; apply.py ignores them):")
    print("  " + ", ".join(extra[:40]) + (" ..." if len(extra) > 40 else ""))
if over:
    print(f"\nFYI - over-max ({len(over)}): apply.py shrinks these to fit. Do NOT re-loop — "
          f"shorten one ONLY if apply.py later reports it as real OVERFLOW:")
    for k, ln, mx, s, v in over[:40]: print(f"  {k}: {ln}/{mx} | src {s!r} | yours {v!r}")
if identical:
    print(f"\nFYI - identical to source ({len(identical)}). Confirm proper nouns/codes/fragments:")
    for k, v in identical[:80]: print(f"  {k}: {v!r}")
if hard:
    print("\nRESULT: problems found - fix translations.json and run check.py again.", file=sys.stderr)
    sys.exit(2)
print("\nRESULT: clean - safe to run apply.py.")

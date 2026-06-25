#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract.py — pdf-translate skill, stage 1. Usage: python3 extract.py <input.pdf> <workdir> [--workers N]"""
import sys, os, re, json, subprocess
from collections import OrderedDict, defaultdict
def ensure(mod, pip=None):
    try: return __import__(mod)
    except ImportError: pass
    subprocess.run([sys.executable,"-m","pip","install","--quiet","--break-system-packages",pip or mod],check=False)
    import site
    for p in site.getusersitepackages() if isinstance(site.getusersitepackages(),list) else [site.getusersitepackages()]:
        if p not in sys.path: sys.path.insert(0, p)
    return __import__(mod)
fitz = ensure("fitz","pymupdf")
_args, WORKERS_OVERRIDE = [], None
_i = 1
while _i < len(sys.argv):
    _a = sys.argv[_i]
    if _a == "--workers" and _i+1 < len(sys.argv): WORKERS_OVERRIDE = int(sys.argv[_i+1]); _i += 2
    elif _a.startswith("--workers="): WORKERS_OVERRIDE = int(_a.split("=",1)[1]); _i += 1
    else: _args.append(_a); _i += 1
SRC, WORK = _args[0], _args[1]
os.makedirs(WORK, exist_ok=True)
doc = fitz.open(SRC)
if doc.needs_pass:
    print("ERROR: PDF is password-protected. Ask the user for the password or an unlocked copy.", file=sys.stderr); sys.exit(4)
n_words = sum(len(p.get_text("words")) for p in doc)
img_cover = 0.0
for p in doc:
    a = 0.0
    for img in p.get_images(full=True):
        for r in p.get_image_rects(img[0]): a += abs(r)
    img_cover += min(1.0, a / abs(p.rect))
img_cover /= max(1, len(doc))
if n_words < 5*len(doc) and img_cover > 0.5:
    print("ERROR: this PDF appears to be scanned (no usable text layer). v1 of this skill translates text-layer PDFs only — tell the user an OCR lane is needed for scanned documents.", file=sys.stderr); sys.exit(3)
def is_symbol_font(f): return any(k in f for k in ("OpenSymbol","Symbol","Wingdings","Dingbat","ZapfD"))
units = []
for pno, page in enumerate(doc):
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] != 0: continue
        cur = None
        for l in b["lines"]:
            ly0, ly1 = l["bbox"][1], l["bbox"][3]
            if l["dir"] != (1.0, 0.0): continue
            prev = None
            for s in l["spans"]:
                sym = is_symbol_font(s["font"])
                if not s["text"].strip() and not sym: prev = s; continue
                style = (s["font"], round(s["size"],1), s["color"])
                new = True
                if cur and cur["style"]==style and not sym and not cur["bullet"]:
                    same = abs(ly0 - cur["ly0"]) < 2
                    if same and prev is not None and s["bbox"][0]-cur["lx1"] < 4: new = False
                    elif not same and abs(s["bbox"][0]-cur["x0"]) < 6 and ly0-cur["ly1"] < 0.9*s["size"]: new = False
                if new:
                    if cur: units.append(cur)
                    cur = {"page":pno,"style":style,"font":s["font"],"size":s["size"],"color":s["color"],
                           "text":s["text"],"bbox":list(s["bbox"]),"x0":s["bbox"][0],
                           "lx1":s["bbox"][2],"ly0":ly0,"ly1":ly1,"bullet":sym,"n_lines":1,"fy0":ly0}
                else:
                    if abs(ly0 - cur["ly0"]) < 2: cur["text"] += s["text"]
                    else:
                        nxt = s["text"].lstrip()
                        if cur["text"].rstrip().endswith("-") and nxt[:1].islower(): cur["text"] = cur["text"].rstrip() + s["text"]
                        elif nxt[:1] in "•·▪◦‣": cur["text"] += "\n" + nxt
                        else: cur["text"] += " " + s["text"]
                        cur["n_lines"] += 1
                    bb = cur["bbox"]
                    cur["bbox"] = [min(bb[0],s["bbox"][0]),min(bb[1],s["bbox"][1]),max(bb[2],s["bbox"][2]),max(bb[3],s["bbox"][3])]
                    cur["lx1"],cur["ly0"],cur["ly1"] = s["bbox"][2], ly0, ly1
                prev = s
        if cur: units.append(cur)
if not units: print("ERROR: no text units found.", file=sys.stderr); sys.exit(5)
norm = lambda t: re.sub(r"[ \t]*\n[ \t]*","\n",re.sub(r"[^\S\n]+"," ",t)).strip()
for i, u in enumerate(units):
    u["id"] = f"u{i:04d}"; u["text"] = norm(u["text"])
    u["pitch"] = round((u["ly0"]-u["fy0"])/(u["n_lines"]-1),2) if u["n_lines"]>1 else None
    for k in ("style","lx1","ly0","ly1","fy0","x0"): u.pop(k, None)
EPS = 0.9
pages_units = defaultdict(list)
for u in units:
    if not u["bullet"]: pages_units[u["page"]].append(u)
def overlap(a,b):
    lo,hi = max(a[1],b[1]),min(a[3],b[3])
    return max(0.0,hi-lo)/max(1e-6,min(a[3]-a[1],b[3]-b[1]))
for pno, pus in pages_units.items():
    pw = doc[pno].rect.width
    def clusters(vals_rows):
        buck = defaultdict(set)
        for v,row in vals_rows: buck[round(v/EPS)].add(round(row))
        return {k for k,rows in buck.items() if len(rows)>=3}
    L = clusters([(u["bbox"][0],u["bbox"][1]) for u in pus])
    R = clusters([(u["bbox"][2],u["bbox"][1]) for u in pus])
    C = clusters([((u["bbox"][0]+u["bbox"][2])/2,u["bbox"][1]) for u in pus])
    for u in pus:
        x0,y0,x1,y1 = u["bbox"]; cx = (x0+x1)/2
        inL,inR,inC = (round(x0/EPS) in L),(round(x1/EPS) in R),(round(cx/EPS) in C)
        short = len(u["text"])<=45 and u["n_lines"]==1
        if inL: u["align"] = "left"
        elif abs(cx-pw/2)<2.5 and x0>60: u["align"] = "center"
        elif inR and short: u["align"] = "right"
        elif inC and short: u["align"] = "center"
        elif short: u["align"] = "center-self"
        else: u["align"] = "left"
        gl, gr = x0-28, pw-28-x1
        for v in pus:
            if v is u or overlap(u["bbox"],v["bbox"])<0.5: continue
            if v["bbox"][0]>=x1-0.5: gr = min(gr, v["bbox"][0]-x1-4)
            if v["bbox"][2]<=x0+0.5: gl = min(gl, x0-v["bbox"][2]-4)
        u["cap_l"],u["cap_r"] = round(max(0,gl),1),round(max(0,gr),1)
NUMERIC = re.compile(r"^[\s\u00a0\u202f\d.,()%+\-–—:/€$£¥₹]*\d[\s\u00a0\u202f\d.,()%+\-–—:/€$£¥₹]*$")
CODEISH = re.compile(
    r"^(\d{4}[A-Z]{0,2}"
    r"|[A-Z]{0,3}\d+\s*[-–—]\s*[A-Z]{0,3}\d+"
    r"|[A-Z]{1,4}-?\d+"
    r"|https?://\S+|\S+@\S+\.\S+"
    r"|[•·▪–—\-|/]+)$")
_ROMAN = re.compile(r"[IVXLC]{1,5}|[ivxlc]{1,5}")
def passthrough(t):
    s = t.strip()
    if not s: return False
    if not any(c.isalpha() for c in s): return True
    if len(s)==1 and s.isascii(): return True
    if len(s)<=5 and _ROMAN.fullmatch(s): return True
    return False
def tier(t):
    if NUMERIC.match(t): return "numeric"
    if CODEISH.match(t): return "code"
    if passthrough(t): return "verbatim"
    return "llm"
tkeys, by_text = {}, OrderedDict()
for u in units:
    if u["bullet"]: u["tier"] = "bullet"; continue
    u["tier"] = tier(u["text"])
    if u["tier"]=="llm":
        if u["text"] not in by_text:
            k = str(len(by_text)+1)
            by_text[u["text"]] = k
        u["tkey"] = by_text[u["text"]]
todo = [{"k":k,"s":t,"max":max(int(len(t)*1.35)+6,18)} for t,k in by_text.items()]
json.dump({"src":os.path.abspath(SRC),"units":units},open(f"{WORK}/slots.json","w",encoding="utf-8"),ensure_ascii=False)
json.dump(todo,open(f"{WORK}/to_translate.json","w",encoding="utf-8"),ensure_ascii=False,indent=0)
BATCH = os.path.join(WORK,"batches")
if os.path.isdir(BATCH):
    for f in os.listdir(BATCH):
        if re.match(r"(batch_\d+|shard\d+_\d+)\.json$",f): os.remove(os.path.join(BATCH,f))
os.makedirs(BATCH, exist_ok=True)
_mp = os.path.join(WORK,"shards.json")
if os.path.exists(_mp): os.remove(_mp)
BUDGET = 30000
def _pack(entries):
    files,cur,cur_len = [],[],2
    for e in entries:
        s = json.dumps(e,ensure_ascii=False)
        if cur and cur_len+len(s)+2>BUDGET: files.append(cur); cur,cur_len = [],2
        cur.append(e); cur_len += len(s)+2
    if cur or not files: files.append(cur)
    return files
def _write(entries, prefix):
    paths = []
    for fi,chunk in enumerate(_pack(entries),1):
        name = f"{prefix}_{fi:02d}.json"
        body = ",\n".join(json.dumps(e,ensure_ascii=False) for e in chunk)
        open(os.path.join(BATCH,name),"w",encoding="utf-8").write("[\n"+body+"\n]\n")
        paths.append(f"batches/{name}")
    return paths
V = sum(len(e["s"]) for e in todo)
# Shard so each shard fits a SINGLE Google translate_text call (30k-codepoint limit).
# We send only the text (not JSON), so the budget applies to sum(len(s)); CHARS_PER_SHARD
# of 25000 leaves headroom and yields ~3-4 shards for a typical 50-page doc.
# ceil() guarantees no shard exceeds the target. Tunable via env.
MAX_WORKERS = int(os.environ.get("PDF_TRANSLATE_MAX_SHARDS", "32"))
CHARS_PER_SHARD = int(os.environ.get("PDF_TRANSLATE_CHARS_PER_SHARD", "25000"))
if WORKERS_OVERRIDE is not None: N = max(1, WORKERS_OVERRIDE)
elif not todo: N = 1
else: N = min(max(-(-V // CHARS_PER_SHARD), 1), MAX_WORKERS)
N = min(N, len(todo)) if todo else 1
PARALLEL = N > 1
if not PARALLEL:
    serial_files = _write(todo,"batch") if todo else []
else:
    def _shard(entries, n):
        total = sum(len(e["s"]) for e in entries); tgt = total/n
        blocks,cur,cur_chars = [],[],0
        for idx,e in enumerate(entries):
            cur.append(e); cur_chars += len(e["s"])
            left = len(entries)-idx-1; opening_left = n-len(blocks)-1
            if len(blocks)<n-1 and cur_chars>=tgt and left>opening_left:
                blocks.append(cur); cur,cur_chars = [],0
        if cur: blocks.append(cur)
        if len(blocks)>=2 and sum(len(e["s"]) for e in blocks[-1])<0.4*tgt:
            tail = blocks.pop(); blocks[-1].extend(tail)
        return blocks
    blocks = _shard(todo, N)
    manifest = {"n_workers":len(blocks),"total_chars":V,"budget_per_file":BUDGET,"shards":[]}
    for si,blk in enumerate(blocks,1):
        manifest["shards"].append({"id":si,"first_key":blk[0]["k"],"last_key":blk[-1]["k"],
            "n_strings":len(blk),"chars":sum(len(e["s"]) for e in blk),
            "out":f"tr_part{si}.json","files":_write(blk,f"shard{si:02d}")})
    json.dump(manifest,open(_mp,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
auto = sum(1 for u in units if u.get("tier") in ("numeric","code","verbatim","bullet"))
img_pages = []
for pno, p in enumerate(doc):
    parea = abs(p.rect) or 1.0; biggest = 0.0
    for img in p.get_images(full=True):
        for r in p.get_image_rects(img[0]): biggest = max(biggest, abs(r)/parea)
    if biggest >= 0.04: img_pages.append(pno+1)
print(f"pages: {len(doc)} | units: {len(units)} | auto-handled: {auto}")
if not PARALLEL:
    print(f"STRINGS TO TRANSLATE: {len(todo)}  ->  read {len(serial_files)} file(s): {WORK}/batches/batch_*.json")
    print(f"  (same strings also in {WORK}/to_translate.json; slots manifest in {WORK}/slots.json)")
else:
    print(f"STRINGS TO TRANSLATE: {len(todo)} ({V} chars)  ->  PARALLEL MODE: {manifest['n_workers']} worker(s)/shard(s)")
    for sh in manifest["shards"]:
        print(f"  shard {sh['id']}: keys {sh['first_key']}-{sh['last_key']} | {sh['n_strings']} strings"
              f" | {sh['chars']} chars | read {len(sh['files'])} file(s): {', '.join(sh['files'])}")
    print(f"  manifest: {WORK}/shards.json (worker for shard k writes tr_part<k>.json) | "
          f"slots: {WORK}/slots.json | full list: {WORK}/to_translate.json")
if img_pages:
    pl = ", ".join(str(p) for p in img_pages)
    print(f"NOTE: large image(s) on page(s) {pl} — may contain text baked into the image layer (not translated).")
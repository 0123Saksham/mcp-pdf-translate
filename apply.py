#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply.py — pdf-translate skill, stage 2. Usage: python3 apply.py <workdir> <output.pdf> --target <lang_code>"""
import sys, os, re, json, html, subprocess, urllib.request, tempfile, shutil, time
def ensure(mod, pip=None):
    try: return __import__(mod)
    except ImportError: pass
    subprocess.run([sys.executable,"-m","pip","install","--quiet","--break-system-packages",pip or mod],check=False)
    import site
    for p in site.getusersitepackages() if isinstance(site.getusersitepackages(),list) else [site.getusersitepackages()]:
        if p not in sys.path: sys.path.insert(0, p)
    return __import__(mod)
fitz = ensure("fitz","pymupdf")
args = sys.argv[1:]
WORK, OUT = args[0], args[1]
def opt(name, default=None): return args[args.index(name)+1] if name in args else default
TARGET = (opt("--target","en") or "en").lower()
_wopt = opt("--workers"); WORKERS_OVERRIDE = int(_wopt) if _wopt else None
LOCALE = {
    "en":(".",",",False),"zh":(".",",",False),"ja":(".",",",False),"ko":(".",",",False),
    "th":(".",",",False),"he":(".",",",False),"ar":(".",",",False),
    "hi":(".",",",False),"bn":(".",",",False),"ta":(".",",",False),
    "te":(".",",",False),"gu":(".",",",False),"kn":(".",",",False),
    "ml":(".",",",False),"mr":(".",",",False),"pa":(".",",",False),
    "tr":(",",".",False),"es":(",",".",False),"it":(",",".",False),
    "pt":(",",".",False),"nl":(",",".",False),"el":(",",".",False),
    "de":(",",".",True),"pl":(",","\u00a0",True),"fr":(",","\u00a0",True),
    "ru":(",","\u00a0",False),"uk":(",","\u00a0",False),"bg":(",","\u00a0",False),
}
_prof = LOCALE.get(TARGET)
DEC = opt("--decimal", _prof[0] if _prof else None)
THO = opt("--thousands", _prof[1] if _prof else None)
PCT_SPACE = _prof[2] if _prof else None
RENDER = [int(x) for x in opt("--render","").split(",") if x] if opt("--render") else []
# Worker mode (portable parallelism, no fork): render only pages a..b (0-based incl) to PART, then exit.
RENDER_RANGE = None
if "--render-range" in args:
    _ri = args.index("--render-range"); RENDER_RANGE = (int(args[_ri+1]), int(args[_ri+2]), args[_ri+3])
_HAS_DEC = "--decimal" in args
_HAS_THO = "--thousands" in args
_t0 = time.monotonic()
slots = json.load(open(f"{WORK}/slots.json",encoding="utf-8"))
units, SRC = slots["units"], slots["src"]
tpath = f"{WORK}/translations.json"
T = json.load(open(tpath,encoding="utf-8")) if os.path.exists(tpath) else {}
need = {u["tkey"] for u in units if u.get("tier")=="llm"}
missing = sorted(k for k in need if not str(T.get(k,"")).strip())
if missing:
    print(f"MISSING {len(missing)} translations in {tpath}: {missing[:25]}"
          f"{' …' if len(missing)>25 else ''}",file=sys.stderr); sys.exit(2)
_T_LOAD = time.monotonic() - _t0
def convert_number(s):
    if DEC is not None or THO is not None:
        s = re.sub(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}\b)","\u0001",s)
        s = re.sub(r"(?<=\d),(?=\d{3}\b)","\u0001",s)
        s = re.sub(r"(?<=\d)[.,](?=\d(?!\d{2}\b))","\u0002",s)
        s = re.sub(r"(?<=\d),(?=\d)","\u0002",s)
        s = s.replace("\u0001",THO or "").replace("\u0002",DEC or ".")
    if PCT_SPACE is True: s = re.sub(r"(?<=\d)[ \u00a0\u202f]*%","\u00a0%",s)
    elif PCT_SPACE is False: s = re.sub(r"(?<=\d)[ \u00a0\u202f]+%","%",s)
    return s
SCRIPT = {
    "hi":("\u0915","NotoSansDevanagari"),"mr":("\u0915","NotoSansDevanagari"),
    "ne":("\u0915","NotoSansDevanagari"),"bn":("\u0995","NotoSansBengali"),
    "ta":("\u0b95","NotoSansTamil"),"te":("\u0c15","NotoSansTelugu"),
    "gu":("\u0a95","NotoSansGujarati"),"kn":("\u0c95","NotoSansKannada"),
    "ml":("\u0d15","NotoSansMalayalam"),"pa":("\u0a15","NotoSansGurmukhi"),
    "th":("\u0e01","NotoSansThai"),"he":("\u05d0","NotoSansHebrew"),
    "ar":("\u0627","NotoSansArabic"),"fa":("\u0627","NotoSansArabic"),
    "ur":("\u0627","NotoSansArabic"),"el":("\u03b1","NotoSans"),
    "ru":("\u0436","NotoSans"),"uk":("\u0436","NotoSans"),"bg":("\u0436","NotoSans"),
    "ka":("\u10d0","NotoSansGeorgian"),"hy":("\u0561","NotoSansArmenian"),
    "ko":("\ud55c",None),"ja":("\u3042",None),"zh":("\u4e2d",None),
}
CJK_URL = {"zh":("SimplifiedChinese","NotoSansCJKsc"),"ja":("Japanese","NotoSansCJKjp"),"ko":("Korean","NotoSansCJKkr")}
RTL = {"ar","fa","ur","he"}
FDIR = os.path.join(WORK,"fonts"); os.makedirs(FDIR, exist_ok=True)
def covers(path, ch):
    try: return fitz.Font(fontfile=path).has_glyph(ord(ch))>0
    except Exception: return False
def fetch(url, dest):
    if not os.path.exists(dest):
        print(f"one-time font download: {os.path.basename(dest)}")
        urllib.request.urlretrieve(url, dest)
    return dest
def resolve_fonts():
    probe = SCRIPT.get(TARGET,("A",None))[0]
    try:
        p = subprocess.run(["fc-match","-f","%{file}",f":lang={TARGET}"],capture_output=True,text=True,timeout=5).stdout.strip()
        if p and covers(p,probe):
            pb = subprocess.run(["fc-match","-f","%{file}",f":lang={TARGET}:weight=bold"],capture_output=True,text=True,timeout=5).stdout.strip()
            return _faces(p, pb if pb and covers(pb,probe) else p)
    except Exception: pass
    dj = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(dj) and covers(dj,probe): return _faces(dj, dj.replace("Sans.ttf","Sans-Bold.ttf"))
    if TARGET in CJK_URL:
        sub,fam = CJK_URL[TARGET]
        base = f"https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/{sub}/"
        r = fetch(base+f"{fam}-Regular.otf",os.path.join(FDIR,f"{fam}-Regular.otf"))
        b = fetch(base+f"{fam}-Bold.otf",os.path.join(FDIR,f"{fam}-Bold.otf"))
        return _faces(r,b)
    fam = SCRIPT.get(TARGET,("A","NotoSans"))[1] or "NotoSans"
    base = f"https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/{fam}/hinted/ttf/"
    r = fetch(base+f"{fam}-Regular.ttf",os.path.join(FDIR,f"{fam}-Regular.ttf"))
    b = fetch(base+f"{fam}-Bold.ttf",os.path.join(FDIR,f"{fam}-Bold.ttf"))
    return _faces(r,b)
def _faces(reg, bold):
    css = (f"@font-face{{font-family:tgt;src:url({os.path.basename(reg)});}}"
           f"@font-face{{font-family:tgt;font-weight:bold;src:url({os.path.basename(bold)});}}")
    for f in {reg,bold}:
        dst = os.path.join(FDIR,os.path.basename(f))
        if not os.path.exists(dst): open(dst,"wb").write(open(f,"rb").read())
    return css, "tgt, sans-serif"
_t1 = time.monotonic()
FACE_CSS, FAMILY = resolve_fonts()
_T_FONTS = time.monotonic() - _t1
ARCHIVE = fitz.Archive(FDIR)
try: ensure("fontTools","fonttools")
except Exception: pass
def rtl_prepare(t):
    try:
        ar = ensure("arabic_reshaper"); bidi = ensure("bidi","python-bidi")
        from bidi.algorithm import get_display
        return get_display(ar.reshape(t))
    except Exception: return t
def style_of(u):
    w = "bold" if "Bold" in u["font"] else "normal"
    s = "italic" if ("Italic" in u["font"] or "Oblique" in u["font"]) else "normal"
    return (w,s)
def lh_of(u):
    ml = u["n_lines"]>1
    lh = (u["pitch"]/u["size"]) if (ml and u.get("pitch") and u["size"]) else 1.18
    return max(1.0, min(1.6, lh))
def make_rect_and_align(u):
    x0,y0,x1,y1 = u["bbox"]; ml = u["n_lines"]>1; al = u.get("align","left")
    capl,capr = u.get("cap_l",28),u.get("cap_r",12)
    if al=="center":
        g = min(40,capl,capr); return fitz.Rect(x0-g,y0-0.7,x1+g,y1+1.6),"center"
    if al=="center-self":
        g = min(20,capl,capr); return fitz.Rect(x0-g,y0-0.7,x1+g,y1+1.6),"center"
    if al=="right": return fitz.Rect(x0-min(34,capl),y0-0.7,x1+0.5,y1+1.6),"right"
    g = min(2.5 if ml else 14,capr); return fitz.Rect(x0-0.5,y0-0.7,x1+g,y1+1.6),"left"
def div_html(u, tr, css_al, lh):
    body = html.escape(tr).replace("\n","<br/>"); w,s = style_of(u)
    return (f'<div style="font-family:{FAMILY};font-size:{u["size"]:.1f}px;'
            f'color:#{u["color"]:06x};font-weight:{w};font-style:{s};'
            f'line-height:{lh:.2f};margin:0;padding:0;text-align:{css_al};">{body}</div>')
_PUNCT = {".",",",":",";",")","]","\u00bb","!","?","\u2026"}
def _is_punct(tr): return tr.strip() in _PUNCT
def _continues(P, V, left_x, right_margin):
    if V["color"]!=P["color"]: return False
    sz = P["size"]; dsz = abs(V["size"]-sz)
    Px0,Py0,Px1,Py1 = P["bbox"]; Vx0,Vy0,Vx1,Vy1 = V["bbox"]
    on_last = abs(Vy0-(Py1-sz))<=0.5*sz and Vx0>=Px0-1
    if on_last and dsz<=2.0:
        if P["n_lines"]==1 and (Vx0-Px1)>2.0*sz: return False
        return True
    wrap = (Py1-0.1)<Vy0<=Py1+0.85*sz
    if wrap and dsz<=0.8:
        near_left = Vx0<=left_x+0.6*sz; full_prev = Px1>=right_margin-1.5*sz
        return near_left and full_prev
    return False
def build_groups(plan, right_margin):
    runs,cur = [],[]
    for u,tr in plan:
        if not cur: cur = [(u,tr)]; continue
        P = cur[-1][0]; left_x = cur[0][0]["bbox"][0]
        same_last_line = abs(u["bbox"][1]-(P["bbox"][3]-P["size"]))<=0.6*P["size"]
        joinable = (P.get("tier")=="llm" and u.get("align")!="right"
            and ((u.get("tier")=="llm" and _continues(P,u,left_x,right_margin))
                 or (u.get("tier")=="verbatim" and _is_punct(tr) and same_last_line)))
        if joinable: cur.append((u,tr))
        else: runs.append(cur); cur = [(u,tr)]
    if cur: runs.append(cur)
    groups = []
    for run in runs:
        llm = [m for m in run if m[0].get("tier")=="llm"]
        styles = {style_of(m[0]) for m in llm}; sizes = [m[0]["size"] for m in llm]
        if len(run)>=2 and len(llm)>=2 and len(styles)>=2 and (max(sizes)-min(sizes))<=2.0:
            groups.append(run)
        else: groups.extend([m] for m in run)
    return groups
def render_group(page, members):
    us = [m[0] for m in members]
    x0 = min(u["bbox"][0] for u in us); y0 = min(u["bbox"][1] for u in us)
    x1 = max(u["bbox"][2] for u in us); y1 = max(u["bbox"][3] for u in us)
    capr = min(u.get("cap_r",12) for u in us)
    rect = fitz.Rect(x0-0.5,y0-0.7,x1+min(14,capr),y1+1.6); base = members[0][0]
    parts = []
    for i,(u,tr) in enumerate(members):
        if TARGET in RTL and u["tier"]=="llm": tr = rtl_prepare(tr)
        seg = html.escape(tr).replace("\n","<br/>"); w,s = style_of(u)
        span = f'<span style="font-weight:{w};font-style:{s};font-size:{u["size"]:.1f}px;">{seg}</span>'
        parts.append(span if i==0 else (("" if _is_punct(tr) else " ")+span))
    h = (f'<div style="font-family:{FAMILY};font-size:{base["size"]:.1f}px;'
         f'color:#{base["color"]:06x};line-height:{lh_of(base):.2f};margin:0;'
         f'padding:0;text-align:left;">{"".join(parts)}</div>')
    return page.insert_htmlbox(rect,h,css=FACE_CSS,archive=ARCHIVE,scale_low=0.5)
def render_page_range(doc, p0, p1):
    overflow,shrunk,identical,reflowed = [],[],set(),[]
    for pno in range(p0,p1+1):
        page = doc[pno]; pus = [u for u in units if u["page"]==pno]; plan = []
        for u in pus:
            t = u["tier"]
            if t=="bullet": tr = "\u2022"
            elif t=="numeric": tr = convert_number(u["text"])
            elif t=="code" or t=="verbatim": tr = u["text"]
            else:
                tr = str(T[u["tkey"]])
                if tr.strip()==u["text"].strip(): identical.add(u["tkey"])
            plan.append((u,tr))
        for u,_ in plan: page.add_redact_annot(fitz.Rect(u["bbox"]))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        right_margin = max((u["bbox"][2] for u in pus),default=0.0)
        for group in build_groups(plan,right_margin):
            if len(group)==1:
                u,tr = group[0]; rect,css_al = make_rect_and_align(u)
                if TARGET in RTL and u["tier"]=="llm": tr = rtl_prepare(tr)
                h = div_html(u,tr,css_al,lh_of(u))
                spare,scale = page.insert_htmlbox(rect,h,css=FACE_CSS,archive=ARCHIVE,scale_low=0.5)
                label = u["text"][:40]
            else:
                spare,scale = render_group(page,group)
                label = "\u21aa "+group[0][0]["text"][:38]; reflowed.append((pno+1,len(group)))
            if spare<0: overflow.append((pno+1,label))
            elif scale<0.999: shrunk.append((pno+1,round(scale,2),label))
    return overflow,shrunk,identical,reflowed
N_PAGES = fitz.open(SRC).page_count
# Production MCP server allows only a couple of child processes here, so the render
# worker pool is capped low (default 3). Higher = faster on multi-core but more
# subprocesses; set PDF_APPLY_MAX_WORKERS=1 to render fully in-process (no children).
APPLY_MAX_WORKERS = int(os.environ.get("PDF_APPLY_MAX_WORKERS", "3"))
PAGES_PER_WORKER = int(os.environ.get("PDF_APPLY_PAGES_PER_WORKER", "8"))
def _nworkers(npages):
    if WORKERS_OVERRIDE is not None: return max(1, WORKERS_OVERRIDE)
    cpu = os.cpu_count() or 4
    return max(1, min(APPLY_MAX_WORKERS, cpu, (npages + PAGES_PER_WORKER - 1)//PAGES_PER_WORKER))
def page_chunks(n, lo=0, hi=None):
    hi = N_PAGES-1 if hi is None else hi
    upp = {}
    for u in units: upp[u["page"]] = upp.get(u["page"],0)+1
    w = {p: upp.get(p,0) for p in range(lo, hi+1)}
    target = (sum(w.values()) or 1)/n
    chunks,start,acc = [],lo,0
    for p in range(lo, hi+1):
        acc += w[p]; pages_left = hi-p; chunks_left = n-len(chunks)-1
        if len(chunks)<n-1 and acc>=target and pages_left>chunks_left:
            chunks.append((start,p)); start = p+1; acc = 0
    chunks.append((start, hi))
    return chunks
def _render_range(p0, p1):
    """Render pages p0..p1 (0-based incl) across parallel subprocesses (portable; no fork).
    Returns (doc, overflow, shrunk, identical, reflowed). Falls back to serial for small ranges."""
    n = _nworkers(p1 - p0 + 1)
    if n > 1 and p1 > p0:
        tmpdir = tempfile.mkdtemp(prefix="pdftrans_")
        try:
            chunks = page_chunks(n, p0, p1)
            extra = []
            if _HAS_DEC and DEC is not None: extra += ["--decimal", DEC]
            if _HAS_THO and THO is not None: extra += ["--thousands", THO]
            procs = []
            for i,(a,b) in enumerate(chunks):
                part = os.path.join(tmpdir, f"part_{i:02d}.pdf")
                cmd = [sys.executable, os.path.abspath(__file__), WORK, OUT,
                       "--target", TARGET, *extra, "--render-range", str(a), str(b), part]
                procs.append((a, b, part, subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)))
            print(f"rendering pages {p0+1}-{p1+1} across {len(procs)} subprocess worker(s): "
                  + ", ".join(f"{a+1}-{b+1}" for a,b,_,_ in procs))
            merged = fitz.open(); ov,sh,idk,rf = [],[],set(),[]
            errors = []
            for a,b,part,pr in procs:
                _o, _e = pr.communicate()
                meta = part + ".meta.json"
                if pr.returncode != 0 or not os.path.exists(part) or not os.path.exists(meta):
                    tail = _e.decode("utf-8","replace")[-400:] if _e else ""
                    errors.append(f"worker {a+1}-{b+1} failed (rc={pr.returncode}): {tail}")
                    continue
                m = json.load(open(meta, encoding="utf-8"))
                pp = fitz.open(part); merged.insert_pdf(pp); pp.close()
                ov += [tuple(x) for x in m["overflow"]]
                sh += [tuple(x) for x in m["shrunk"]]
                idk |= set(m["identical"])
                rf += [tuple(x) for x in m["reflowed"]]
            if errors:
                merged.close(); raise RuntimeError("; ".join(errors))
            return merged, ov, sh, idk, rf
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    d = fitz.open(SRC); ov,sh,idk,rf = render_page_range(d, p0, p1)
    return d, ov, sh, idk, rf
def _paths_for(out):
    i = max(out.rfind("/"), out.rfind("\\"))            # handle / and \ (OUT may be a Windows path on a Linux mount)
    if i >= 0: d, sep, name = out[:i], out[i], out[i+1:]
    else: d, sep, name = ".", "/", out
    j = name.rfind(".")
    base, ext = (name[:j], name[j:]) if j > 0 else (name, ".pdf")
    tmp = f"{d}{sep}.pdftrans.{os.getpid()}{ext}"
    return tmp, [out] + [f"{d}{sep}{base}_{k}{ext}" for k in range(1, 6)]
def _save_doc(doc):
    # Save to a fresh temp file, then atomically rename into place. This never overwrites a
    # locked OUT in place (the in-place save is what crashed when the previous run held the
    # file open); if OUT itself is locked, fall back to OUT_1.pdf, OUT_2.pdf, ... cleanly.
    tmp, dests = _paths_for(OUT)
    try:
        if os.path.exists(tmp): os.remove(tmp)
    except OSError: pass
    doc.save(tmp, garbage=4, deflate=True, clean=True)
    for i, dest in enumerate(dests):
        try:
            os.replace(tmp, dest)
            if i: print(f"note: '{OUT}' was locked or unwritable; wrote '{dest}' instead.", file=sys.stderr)
            return dest
        except OSError: continue
    print(f"note: could not write '{OUT}' or any fallback name; output left at '{tmp}'.", file=sys.stderr)
    return tmp
def _qa_overflow(overflow):
    if not overflow: return
    tiny = lambda t: len(t.replace("\u21aa","").strip()) <= 3
    real = [(p,t) for p,t in overflow if not tiny(t)]
    frags = [(p,t) for p,t in overflow if tiny(t)]
    if real:
        print(f"OVERFLOW ({len(real)}) - did not fit even at 50%; shorten these:")
        for p,t in real: print(f"  p{p} | {t}")
    if frags:
        ex = ", ".join(repr(t.replace("\u21aa","").strip()) for _,t in frags[:5])
        print(f"minor: {len(frags)} tiny fragment(s) overflowed ({ex}) - PDF artifacts (1-3 chars), safe to ignore.")
def _finish(doc, overflow, shrunk, identical, reflowed):
    try: ensure("fontTools","fonttools"); doc.subset_fonts()
    except Exception: pass
    dest = _save_doc(doc)
    print(f"OK wrote {dest}  ({os.path.getsize(dest)//1024} KB)")
    print(f"units: {len(units)} | translated keys: {len(need)} | identical-to-source: {len(identical)}")
    if reflowed:
        pages = sorted({p for p,_ in reflowed})
        print(f"inline-reflow: {len(reflowed)} on {len(pages)} page(s): {pages[:20]}")
    if shrunk:
        print(f"shrunk-to-fit ({len(shrunk)}):")
        for p,sc,t in shrunk[:12]: print(f"  p{p} x{sc} | {t}")
    _qa_overflow(overflow)
    if TARGET in RTL: print("note: RTL target — shaping applied best-effort; render a page to verify.")
    for p in RENDER:
        fp = f"{WORK}/render_p{p}.png"; fitz.open(dest)[p-1].get_pixmap(dpi=110).save(fp); print(f"render: {fp}")
def _print_timing(t_render, t_finish):
    total = _T_LOAD + _T_FONTS + t_render + t_finish
    print()
    print("=" * 50)
    print("TIMING")
    print(f"  load data:     {_T_LOAD:6.2f}s")
    print(f"  resolve fonts: {_T_FONTS:6.2f}s")
    print(f"  render pages:  {t_render:6.2f}s")
    print(f"  save + finish: {t_finish:6.2f}s")
    print(f"  TOTAL:         {total:6.2f}s")
    print("=" * 50)
if __name__ == "__main__":
    # Worker mode: render just our page range, write a partial PDF + sidecar meta, exit.
    if RENDER_RANGE is not None:
        _a, _b, _part = RENDER_RANGE
        _d = fitz.open(SRC); _ov,_sh,_idk,_rf = render_page_range(_d, _a, _b)
        _out = fitz.open(); _out.insert_pdf(_d, from_page=_a, to_page=_b)
        try: _out.subset_fonts()
        except Exception: pass
        _out.save(_part, garbage=4, deflate=True); _out.close(); _d.close()
        json.dump({"overflow": _ov, "shrunk": _sh, "identical": sorted(_idk), "reflowed": _rf},
                  open(_part + ".meta.json", "w", encoding="utf-8"), ensure_ascii=False)
        sys.exit(0)
    t_render = time.monotonic()
    try:
        doc, ov, sh, idk, rf = _render_range(0, N_PAGES-1)
    except Exception as e:
        print(f"note: parallel reassembly failed ({e}); falling back to serial.", file=sys.stderr)
        doc = fitz.open(SRC); ov,sh,idk,rf = render_page_range(doc, 0, N_PAGES-1)
    t_render = time.monotonic() - t_render
    t_finish = time.monotonic()
    _finish(doc, ov, sh, idk, rf)
    t_finish = time.monotonic() - t_finish
    _print_timing(t_render, t_finish)

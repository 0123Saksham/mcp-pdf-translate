#!/usr/bin/env python3
"""Benchmark the full skill flow at different `effort` levels and compare.

Mimics exactly what the pdf-translate-url skill does end to end:
    create_upload_url  ->  PUT to Azure  ->  translate_pdf_url (download +
    extract + translate + check + apply + output upload)  ->  download result

It runs the whole flow once per effort level, in a SEPARATE subprocess each
(so PDF_TRANSLATE_EFFORT is read cleanly at import), records the wall-clock time
of every step plus per-shard (per concurrent API call) translate times, samples
a few source->translation pairs for quality comparison, and writes a combined
report to tests/benchmark_results/.

Usage:
    # run the comparison (default efforts: low and high)
    python tests/benchmark_effort.py "D:\\Internship\\skill\\50_pages_french.pdf" en

    # custom effort levels
    python tests/benchmark_effort.py "<pdf>" en --efforts low medium high

    # (internal) single-effort worker — invoked by the orchestrator
    python tests/benchmark_effort.py --worker <effort> <pdf> <lang> <out_json>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MCP_ROOT))

RESULTS_DIR = Path(__file__).resolve().parent / "benchmark_results"
SAMPLE_PAIRS = 12  # how many source->translation pairs to capture for quality review


# --------------------------------------------------------------------------- #
# Worker: run the full flow ONCE for a single effort level
# --------------------------------------------------------------------------- #
def _run_worker(effort: str, pdf_path: str, lang: str, out_json: str) -> int:
    # Effort must be set before translate_lib is imported (it reads it at import).
    os.environ["PDF_TRANSLATE_EFFORT"] = effort

    import asyncio

    import requests

    from server import create_upload_url
    from pipeline import run_pipeline_url

    src = Path(pdf_path)
    steps: dict[str, float] = {}
    result: dict = {"effort": effort, "ok": False}

    # Step 2 (skill): create_upload_url
    t = time.monotonic()
    res = json.loads(asyncio.run(create_upload_url(src.name)))
    steps["create_upload_url"] = round(time.monotonic() - t, 3)
    if not res.get("ok"):
        result["error"] = f"create_upload_url failed: {res}"
        Path(out_json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    # Step 3 (skill): PUT the PDF to Azure
    data = src.read_bytes()
    t = time.monotonic()
    put = requests.put(res["upload_url"], data=data, headers=res["required_headers"], timeout=120)
    steps["azure_upload"] = round(time.monotonic() - t, 3)
    if put.status_code not in (200, 201):
        result["error"] = f"azure upload failed: HTTP {put.status_code}"
        Path(out_json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    # Step 4 (skill): translate_pdf_url — server downloads, runs pipeline, uploads output
    t = time.monotonic()
    out = run_pipeline_url(res["download_url"], lang)
    steps["translate_pdf_url_total"] = round(time.monotonic() - t, 3)
    if not out.get("ok"):
        result["error"] = f"pipeline failed: {out.get('stage')}: {out.get('error')}"
        result["steps"] = steps
        Path(out_json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    # Step 5 (skill): download the translated result
    t = time.monotonic()
    g = requests.get(out["download_link"], timeout=120)
    steps["download_result"] = round(time.monotonic() - t, 3)
    result["result_is_pdf"] = g.content[:4] == b"%PDF"

    # Per-shard (per concurrent API call) timings written by run_translate
    work_dir = Path(out["stats"]["work_dir"])
    shard_timings = []
    st_path = work_dir / "shard_timings.json"
    if st_path.is_file():
        shard_timings = json.loads(st_path.read_text(encoding="utf-8"))

    # Quality sample: pair sources (to_translate.json) with translations (tr_part*.json)
    samples = _collect_samples(work_dir)

    result.update({
        "ok": True,
        "steps_wallclock_s": steps,
        "server_stats": out["stats"],
        "shard_timings": shard_timings,
        "shard_summary": _summarize_shards(shard_timings),
        "samples": samples,
        "download_link": out["download_link"],
    })
    Path(out_json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _collect_samples(work_dir: Path) -> list[dict]:
    """Pair source strings with their translations for quality eyeballing."""
    try:
        sources = {str(e["k"]): e["s"] for e in json.loads((work_dir / "to_translate.json").read_text(encoding="utf-8"))}
    except Exception:
        return []
    translations: dict[str, str] = {}
    for part in sorted(work_dir.glob("tr_part*.json")):
        try:
            translations.update(json.loads(part.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not translations:
        single = work_dir / "translations.json"
        if single.is_file():
            translations.update(json.loads(single.read_text(encoding="utf-8")))

    pairs = []
    for k, src in sources.items():
        tgt = translations.get(k)
        if not tgt:
            continue
        # Prefer non-trivial strings (skip pure numbers / very short)
        if len(src.strip()) < 8:
            continue
        pairs.append({"k": k, "source": src, "translation": tgt})
        if len(pairs) >= SAMPLE_PAIRS:
            break
    return pairs


def _summarize_shards(shard_timings: list[dict]) -> dict:
    if not shard_timings:
        return {}
    times = sorted(s["elapsed_s"] for s in shard_timings)
    n = len(times)
    return {
        "n_shards": n,
        "min_s": times[0],
        "max_s": times[-1],
        "avg_s": round(sum(times) / n, 2),
        "median_s": times[n // 2],
        "total_output_tokens": sum(s["output_tokens"] for s in shard_timings),
        "total_input_tokens": sum(s["input_tokens"] for s in shard_timings),
    }


# --------------------------------------------------------------------------- #
# Orchestrator: run the worker once per effort, then write a comparison report
# --------------------------------------------------------------------------- #
def _run_comparison(pdf_path: str, lang: str, efforts: list[str]) -> int:
    src = Path(pdf_path)
    if not src.is_file():
        print(f"ERROR: PDF not found: {src}", file=sys.stderr)
        return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    runs: list[dict] = []

    for effort in efforts:
        print(f"\n=== Running full flow @ effort={effort} ===", flush=True)
        out_json = RESULTS_DIR / f"_run_{effort}_{stamp}.json"
        env = dict(os.environ, PDF_TRANSLATE_EFFORT=effort)
        proc = subprocess.run(
            [sys.executable, __file__, "--worker", effort, str(src), lang, str(out_json)],
            env=env,
        )
        if proc.returncode != 0 or not out_json.is_file():
            print(f"  effort={effort} run FAILED (exit {proc.returncode})", file=sys.stderr)
            if out_json.is_file():
                runs.append(json.loads(out_json.read_text(encoding="utf-8")))
            continue
        data = json.loads(out_json.read_text(encoding="utf-8"))
        runs.append(data)
        if data.get("ok"):
            t = data["server_stats"]["timing_s"]
            print(f"  done: translate={t.get('translate')}s total_server={t.get('total')}s")

    report_path = RESULTS_DIR / f"benchmark_{stamp}.md"
    _write_report(report_path, src, lang, runs)
    json_path = RESULTS_DIR / f"benchmark_{stamp}.json"
    json_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nReport written to:\n  {report_path}\n  {json_path}")
    return 0


def _write_report(path: Path, src: Path, lang: str, runs: list[dict]) -> None:
    ok_runs = [r for r in runs if r.get("ok")]
    lines: list[str] = []
    lines.append(f"# Effort benchmark — {src.name} -> {lang}")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"PDF: `{src}` ({src.stat().st_size:,} bytes)")
    lines.append("")

    # Step-by-step wall-clock comparison
    lines.append("## Step timings (wall-clock, seconds)")
    lines.append("")
    step_keys = ["create_upload_url", "azure_upload", "translate_pdf_url_total", "download_result"]
    server_keys = ["download", "extract", "translate", "check", "apply", "upload", "total"]
    header = "| Step | " + " | ".join(r["effort"] for r in runs) + " |"
    sep = "|------|" + "|".join(["------"] * len(runs)) + "|"
    lines.append(header)
    lines.append(sep)
    for k in step_keys:
        row = [k] + [str(r.get("steps_wallclock_s", {}).get(k, "-")) for r in runs]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("| _server-side breakdown_ | " + " | ".join([""] * len(runs)) + " |")
    for k in server_keys:
        row = [f"&nbsp;&nbsp;{k}"] + [str(r.get("server_stats", {}).get("timing_s", {}).get(k, "-")) for r in runs]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Per-shard (concurrent API call) summary
    lines.append("## Per-shard translate timings (concurrent API calls)")
    lines.append("")
    header = "| Metric | " + " | ".join(r["effort"] for r in runs) + " |"
    lines.append(header)
    lines.append(sep)
    for metric in ["n_shards", "min_s", "median_s", "avg_s", "max_s", "total_input_tokens", "total_output_tokens"]:
        row = [metric] + [str(r.get("shard_summary", {}).get(metric, "-")) for r in runs]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Full per-shard table per effort (so slow shards are visible)
    for r in ok_runs:
        lines.append(f"### Per-shard detail @ effort={r['effort']}")
        lines.append("")
        lines.append("| shard | entries | elapsed_s | in_tok | out_tok |")
        lines.append("|-------|---------|-----------|--------|---------|")
        for s in r.get("shard_timings", []):
            lines.append(f"| {s['shard_id']} | {s['entries']} | {s['elapsed_s']} | {s['input_tokens']} | {s['output_tokens']} |")
        lines.append("")

    # Quality samples side by side
    lines.append("## Translation quality samples")
    lines.append("")
    for r in ok_runs:
        lines.append(f"### effort={r['effort']}")
        lines.append("")
        for p in r.get("samples", []):
            lines.append(f"- **src:** {p['source']}")
            lines.append(f"  **out:** {p['translation']}")
        lines.append("")

    # Failures
    for r in runs:
        if not r.get("ok"):
            lines.append(f"## FAILED @ effort={r.get('effort')}")
            lines.append(f"`{r.get('error')}`")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        _, _flag, effort, pdf_path, lang, out_json = sys.argv[:6]
        return _run_worker(effort, pdf_path, lang, out_json)

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("lang")
    ap.add_argument("--efforts", nargs="+", default=["low", "high"])
    args = ap.parse_args()
    return _run_comparison(args.pdf, args.lang, args.efforts)


if __name__ == "__main__":
    raise SystemExit(main())

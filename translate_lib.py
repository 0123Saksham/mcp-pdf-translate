"""Parallel translation via Google Cloud Translation v3 (translate_text).

Swapped from Anthropic to Google NMT. Key design points:
  * We send only the source TEXT (one array element per line) to translate_text,
    never JSON — so the 30k-codepoint per-request limit applies to the text alone.
  * Each shard (produced by extract.py, sized to fit one call) becomes one logical
    translation unit; we still defensively sub-batch at MAX_CODEPOINTS so an
    oversized shard can never trigger a 400 INVALID_ARGUMENT.
  * Newlines are preserved EXACTLY: every "s" is split on "\n", each line is
    translated as its own segment, and the lines are rejoined with "\n". This
    guarantees check.py's strict newline count passes.
  * Shards run concurrently. The blocking google client runs in a thread via
    asyncio.to_thread, bounded by a semaphore (mirrors the old async flow without
    needing grpc.aio).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from google.cloud import translate_v3

from config import google_location, google_project

# Speed-over-cost: how many shard calls run at once.
MAX_CONCURRENCY = int(os.environ.get("PDF_TRANSLATE_CONCURRENCY", "16"))
# Hard cap below Google's 30k-codepoint online limit (leaves headroom for safety).
MAX_CODEPOINTS = int(os.environ.get("PDF_TRANSLATE_MAX_CODEPOINTS", "28000"))
# Max number of strings (array elements) per translate_text request.
MAX_SEGMENTS = int(os.environ.get("PDF_TRANSLATE_MAX_SEGMENTS", "1024"))
# Optional source language hint; empty => Google auto-detects.
SOURCE_LANG = os.environ.get("PDF_TRANSLATE_SOURCE_LANG", "").strip()

_DEBUG_LOG = Path(__file__).resolve().parent / "runs" / "pipeline_debug.log"


def _log(msg: str) -> None:
    line = f"[translate] {time.strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with _DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _make_client() -> translate_v3.TranslationServiceClient:
    """Build a (thread-safe) sync client. Uses GOOGLE_APPLICATION_CREDENTIALS if set,
    else Application Default Credentials."""
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_path and Path(key_path).is_file():
        return translate_v3.TranslationServiceClient.from_service_account_file(key_path)
    return translate_v3.TranslationServiceClient()


def _subbatch(texts: list[str], idxs: list[int]) -> list[list[int]]:
    """Greedy pack the given indices into batches under codepoint + segment caps."""
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_cp = 0
    for i in idxs:
        cp = len(texts[i])
        if cp > MAX_CODEPOINTS:
            if cur:
                batches.append(cur)
                cur, cur_cp = [], 0
            batches.append([i])  # oversized single line on its own
            continue
        if cur and (cur_cp + cp > MAX_CODEPOINTS or len(cur) >= MAX_SEGMENTS):
            batches.append(cur)
            cur, cur_cp = [], 0
        cur.append(i)
        cur_cp += cp
    if cur:
        batches.append(cur)
    return batches


def _translate_shard_sync(
    client: translate_v3.TranslationServiceClient,
    parent: str,
    entries: list,
    target: str,
) -> tuple[dict, int]:
    """Translate one shard. Returns ({key: translated_text}, source_codepoints).

    Splits each entry on '\\n' so newline counts are preserved on rejoin.
    Whitespace-only / empty lines are passed through untouched (not sent)."""
    flat: list[str] = []
    spans: list[tuple[str, int, int]] = []  # (key, n_lines, start_index)
    for e in entries:
        s = str(e["s"])
        lines = s.split("\n")
        spans.append((str(e["k"]), len(lines), len(flat)))
        flat.extend(lines)

    out_lines: list[str] = list(flat)  # default: passthrough (covers empty lines)
    translate_idx = [i for i, t in enumerate(flat) if t.strip()]
    src_cp = sum(len(flat[i]) for i in translate_idx)

    for batch in _subbatch(flat, translate_idx):
        contents = [flat[i] for i in batch]
        req = {
            "parent": parent,
            "target_language_code": target,
            "mime_type": "text/plain",
            "contents": contents,
        }
        if SOURCE_LANG:
            req["source_language_code"] = SOURCE_LANG
        resp = client.translate_text(request=req)
        for i, tr in zip(batch, resp.translations):
            out_lines[i] = tr.translated_text

    result: dict[str, str] = {}
    for key, n, start in spans:
        result[key] = "\n".join(out_lines[start:start + n])
    return result, src_cp


async def _run_shard(
    client,
    parent: str,
    shard_id: int,
    entries: list,
    target: str,
    work_dir: Path,
    sem: asyncio.Semaphore,
) -> dict:
    t0 = time.monotonic()
    try:
        async with sem:
            translations, src_cp = await asyncio.to_thread(
                _translate_shard_sync, client, parent, entries, target
            )
    except Exception as e:  # noqa: BLE001
        return {"shard_id": shard_id, "ok": False, "error": str(e),
                "elapsed_s": round(time.monotonic() - t0, 2)}
    (work_dir / f"tr_part{shard_id}.json").write_text(
        json.dumps(translations, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "shard_id": shard_id,
        "ok": True,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "entries": len(entries),
        "source_codepoints": src_cp,
    }


async def run_translate(
    work_dir: Path,
    target: str,
    model: str | None = None,  # accepted for signature parity; unused (standard NMT)
) -> dict:
    """Translate work/ directory with Google. Writes tr_part*.json (check.py merges)."""
    work_dir = work_dir.resolve()
    t0 = time.monotonic()

    parent = f"projects/{google_project()}/locations/{google_location()}"
    client = _make_client()

    shards_path = work_dir / "shards.json"
    if shards_path.exists():
        manifest = json.loads(shards_path.read_text(encoding="utf-8"))
        jobs = []
        for s in manifest["shards"]:
            entries = []
            for batch_rel in s["files"]:
                entries.extend(json.loads((work_dir / batch_rel).read_text(encoding="utf-8")))
            jobs.append((s["id"], entries))
    else:
        todo_path = work_dir / "to_translate.json"
        if not todo_path.exists():
            raise RuntimeError(f"{todo_path} not found — run extract first")
        entries = json.loads(todo_path.read_text(encoding="utf-8"))
        jobs = [(1, entries)] if entries else []

    n_shards = len(jobs)
    if not jobs:
        (work_dir / "translations.json").write_text("{}", encoding="utf-8")
    else:
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        _log(f"translating {n_shards} shard(s), concurrency={MAX_CONCURRENCY}, parent={parent}")
        results = await asyncio.gather(
            *[_run_shard(client, parent, sid, ent, target, work_dir, sem) for sid, ent in jobs],
            return_exceptions=True,
        )
        failures, shard_timings = [], []
        for r in results:
            if isinstance(r, Exception):
                failures.append(str(r))
            elif not r.get("ok"):
                failures.append(r.get("error", "unknown"))
            else:
                shard_timings.append(r)
        if failures:
            raise RuntimeError(f"translate failed: {failures[0]}")
        shard_timings.sort(key=lambda x: x["shard_id"])
        (work_dir / "shard_timings.json").write_text(
            json.dumps(shard_timings, indent=2), encoding="utf-8"
        )

    elapsed = round(time.monotonic() - t0, 2)
    todo = json.loads((work_dir / "to_translate.json").read_text(encoding="utf-8"))
    return {
        "shards": n_shards,
        "strings_translated": len(todo),
        # Google NMT is not token-billed; kept for stats-shape parity with the old API.
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_s": elapsed,
    }

"""Parallel / serial translation via Anthropic API (from translate_poc logic)."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic

from config import api_key, DEFAULT_MODEL

# Speed-over-cost: bound how many API calls run at once (safety against rate limits).
# Set high enough that all shards fire together for a typical doc.
MAX_CONCURRENCY = int(os.environ.get("PDF_TRANSLATE_CONCURRENCY", "32"))
MAX_RETRIES = int(os.environ.get("PDF_TRANSLATE_RETRIES", "3"))
TEMPERATURE = float(os.environ.get("PDF_TRANSLATE_TEMPERATURE", "0"))
# Low effort = fewer output tokens, faster responses. Thinking is disabled, so
# effort only trims text-output spend here (no reasoning to scale).
EFFORT = os.environ.get("PDF_TRANSLATE_EFFORT", "low")

# Append to the same debug log pipeline.py uses (decoupled — no import to avoid a cycle).
_DEBUG_LOG = Path(__file__).resolve().parent / "runs" / "pipeline_debug.log"


def _log(msg: str) -> None:
    line = f"[translate] {time.strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with _DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

SYSTEM_PROMPT = """You translate one shard of a document into the target language.

You will receive a JSON array of entries. Each entry is {"k": <key>, "s": <source text>, "max": <limit>}.
Translate every "s" into the target language.

RULES (non-negotiable):
- Preserve every newline (\\n) in the source exactly — same count, same positions.
- Keep proper nouns, company names, addresses, codes (e.g. "CCAP", "BP 29"), emails, and URLs unchanged.
- Write embedded numbers in the target language's convention (decimal separators, etc.).
- Keep terminology and register consistent throughout the shard.
- Try to stay within "max" chars but do not sacrifice meaning to do so.

For each input entry, return one item whose "k" is that entry's key and whose "t" is the translation."""

USER_TEMPLATE = """Target language: {target}

Translate these strings:
{shard_json}"""

# Schema for the API's structured output (output_config). The shape is enforced
# by the API, so the prompt no longer has to describe the JSON format.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "k": {"type": "string"},
                    "t": {"type": "string"},
                },
                "required": ["k", "t"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


async def _call_once(
    client: AsyncAnthropic,
    entries: list,
    target: str,
    model: str,
) -> tuple[dict, int, int]:
    shard_json = json.dumps(entries, ensure_ascii=False, indent=0)
    user_msg = USER_TEMPLATE.format(target=target, shard_json=shard_json)
    response = await client.messages.create(
        model=model,
        max_tokens=16000,
        temperature=TEMPERATURE,
        thinking={"type": "disabled"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
        },
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    data = json.loads(text)
    translations = {str(item["k"]): item["t"] for item in data["translations"]}
    expected = {str(e["k"]) for e in entries}
    missing = expected - set(translations.keys())
    if missing:
        raise RuntimeError(f"translation missing keys: {sorted(missing)[:20]}")
    return translations, response.usage.input_tokens, response.usage.output_tokens


async def _translate_entries(
    client: AsyncAnthropic,
    entries: list,
    target: str,
    model: str,
    label: str = "",
) -> tuple[dict, int, int]:
    """Call the API with retries on transient errors (429 / overloaded / timeouts)."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await _call_once(client, entries, target, model)
        except Exception as e:  # noqa: BLE001 — retry transient + bad-output failures
            last_err = e
            if attempt >= MAX_RETRIES:
                break
            msg = str(e).lower()
            # Network/server transients AND model-output errors (malformed JSON, missing keys):
            # re-asking the model almost always fixes the latter, so both are worth a retry.
            is_ratelimit = any(s in msg for s in ("overloaded", "rate", "429", "timeout", "timed out", "connection", "529"))
            is_badoutput = isinstance(e, (json.JSONDecodeError, ValueError)) or "missing keys" in msg
            if not (is_ratelimit or is_badoutput):
                break
            backoff = round((2 ** attempt) + random.uniform(0, 0.5), 2)
            kind = "rate-limit" if is_ratelimit else "bad-output"
            _log(f"{label or 'shard'}: retry {attempt+1}/{MAX_RETRIES} ({kind}, backoff {backoff}s): {str(e)[:90]}")
            await asyncio.sleep(backoff)
    raise last_err if last_err else RuntimeError("translate failed")


async def _translate_one_shard(
    client: AsyncAnthropic,
    shard_id: int,
    entries: list,
    target: str,
    model: str,
    work_dir: Path,
    sem: asyncio.Semaphore,
) -> dict:
    t0 = time.monotonic()
    try:
        async with sem:
            translations, in_tok, out_tok = await _translate_entries(
                client, entries, target, model, label=f"shard{shard_id}"
            )
    except Exception as e:
        return {"shard_id": shard_id, "ok": False, "error": str(e), "elapsed_s": round(time.monotonic() - t0, 2)}
    out_path = work_dir / f"tr_part{shard_id}.json"
    out_path.write_text(
        json.dumps(translations, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "shard_id": shard_id,
        "ok": True,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


async def run_translate(
    work_dir: Path,
    target: str,
    model: str | None = None,
) -> dict:
    """Translate work/ directory. Writes tr_part*.json or translations.json."""
    model = model or DEFAULT_MODEL
    client = AsyncAnthropic(api_key=api_key())
    work_dir = work_dir.resolve()
    t0 = time.monotonic()

    shards_path = work_dir / "shards.json"
    total_in = total_out = 0
    n_shards = 0

    if shards_path.exists():
        manifest = json.loads(shards_path.read_text(encoding="utf-8"))
        shards = manifest["shards"]
        n_shards = len(shards)
        jobs = []
        for s in shards:
            entries = []
            for batch_rel in s["files"]:
                entries.extend(json.loads((work_dir / batch_rel).read_text(encoding="utf-8")))
            jobs.append((s["id"], entries))
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        results = await asyncio.gather(
            *[_translate_one_shard(client, sid, ent, target, model, work_dir, sem) for sid, ent in jobs],
            return_exceptions=True,
        )
        failures = []
        entry_counts = {sid: len(ent) for sid, ent in jobs}
        shard_timings = []
        for r in results:
            if isinstance(r, Exception):
                failures.append(str(r))
                continue
            if not r.get("ok"):
                failures.append(r.get("error", "unknown"))
            else:
                total_in += r["input_tokens"]
                total_out += r["output_tokens"]
                shard_timings.append({
                    "shard_id": r["shard_id"],
                    "entries": entry_counts.get(r["shard_id"]),
                    "elapsed_s": r["elapsed_s"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                })
        if failures:
            raise RuntimeError(f"translate failed: {failures[0]}")
        shard_timings.sort(key=lambda x: x["shard_id"])
        (work_dir / "shard_timings.json").write_text(
            json.dumps(shard_timings, indent=2), encoding="utf-8"
        )
    else:
        todo_path = work_dir / "to_translate.json"
        if not todo_path.exists():
            raise RuntimeError(f"{todo_path} not found — run extract first")
        entries = json.loads(todo_path.read_text(encoding="utf-8"))
        if not entries:
            tr_path = work_dir / "translations.json"
            tr_path.write_text("{}", encoding="utf-8")
            n_shards = 0
        else:
            n_shards = 1
            translations, total_in, total_out = await _translate_entries(client, entries, target, model)
            (work_dir / "translations.json").write_text(
                json.dumps(translations, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

    elapsed = round(time.monotonic() - t0, 2)
    todo = json.loads((work_dir / "to_translate.json").read_text(encoding="utf-8"))
    return {
        "shards": n_shards,
        "strings_translated": len(todo),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "elapsed_s": elapsed,
    }

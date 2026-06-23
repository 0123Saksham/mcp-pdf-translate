# pdf-translate MCP — Build Plan

Plan for testing **base64 PDF in → full pipeline → base64 PDF out** via an MCP tool exposed to Claude.

**Status:** Implemented — see `server.py`, `pipeline.py`, `README.md`.

---

## Do you need an MCP server?

**Yes.** An MCP tool is not a standalone script Claude can call by itself. You need:

1. **MCP server process** — long-running Python (or Node) process that speaks the MCP protocol over stdio (Cursor/Claude Desktop) or HTTP/SSE (remote).
2. **Tool handler(s)** — functions the server registers; Claude invokes them with JSON arguments.
3. **Client config** — tell **Claude Desktop** where to launch the server (`claude_desktop_config.json` on Windows).

Claude never runs `extract.py` directly. It calls your tool; the server runs the scripts internally.

---

## What you want to test

```
┌─────────────┐     base64 PDF (~1.45 MB)      ┌──────────────────┐
│   Claude    │ ─────────────────────────────► │  MCP server      │
│  (orchestr.)│                                │  translate_pdf   │
│             │ ◄───────────────────────────── │  (monolithic)    │
└─────────────┘     base64 PDF (~1.5+ MB)      └──────────────────┘
       │                                                    │
       │ decode & save                                      │ decode → work/
       ▼                                                    ▼ extract → translate → check → apply
 translated.pdf                                    (existing scripts)
```

**Your measured sizes (50-page French PDF):**

| | Size |
|---|------|
| Original PDF | 1.09 MB (1,138,874 bytes) |
| Base64 string | 1.45 MB (1,518,500 chars) |
| Round-trip (in + out) | ~3 MB of payload through Claude’s tool channel |

This plan assumes you still want to **run the experiment** to see limits in practice (context, latency, cost, tool-arg size caps).

---

## Honest feasibility (read before building)

| Concern | Impact on base64-in/out design |
|--------|--------------------------------|
| **Token cost** | ~1.5M chars of base64 ≈ hundreds of thousands to ~1M+ tokens **per direction** if the string transits Claude’s context. Tool args often count toward context. |
| **Context window** | May exceed 200K context on Claude.ai for a single call. |
| **Tool latency** | Extract (~3s) + translate (~45–60s) + apply (~40s) = **~90s+** inside one tool call; client may timeout. |
| **Output size** | Returning 1.5MB base64 in tool **result** may hit MCP/transport limits. |
| **LLM decode step** | Server should decode base64 and write the file, then just return the output **path**. Avoid asking Claude to decode or write files; just return the path to the result. |

**What this test still proves:**

- End-to-end wiring (MCP ↔ scripts ↔ API).
- Whether your client accepts ~1.5MB tool arguments.
- Wall-clock for the full pipeline in one shot.

**What to build next if base64 fails** (Phase 2 — same folder, different tool):

- `translate_pdf_path(input_path, target)` — PDF on disk, server returns `output_path`.
- Or `start_job` / `poll_job` — async, no huge payloads through Claude.

This plan implements **Phase 1 (base64)** as you requested, with hooks to add Phase 2 without rewriting scripts.

---

## Proposed folder layout

```
pdf-translate-mcp/
├── PLAN.md                 ← this file
├── README.md               ← setup, Claude Desktop config, how to test
├── requirements.txt        ← mcp, anthropic, pymupdf, …
├── pyproject.toml          ← optional; package entry point
│
├── server.py               ← MCP server entry (stdio)
├── pipeline.py             ← orchestrator: decode → extract → translate → check → apply → encode
├── config.py               ← paths to scripts, API key, temp dir, timeouts
│
├── scripts/                ← symlinks or copies of existing scripts (no rewrite)
│   ├── extract.py          ← from ../extract.py
│   ├── check.py            ← from ../check.py
│   ├── apply.py            ← from ../apply.py
│   └── translate_poc.py    ← from ../translate_poc.py (or import as module)
│
├── tests/
│   ├── test_pipeline_local.py   ← no MCP; base64 round-trip with real PDF
│   └── test_tool_mock.py        ← mock API for fast CI
│
└── claude_desktop_config.example.json ← copy into %APPDATA%\Claude\
```

**Why copy/symlink scripts into `scripts/`:** MCP server runs from its own cwd; stable relative paths avoid breaking when launched from Claude Desktop.

**Alternative:** `pipeline.py` imports from parent via `sys.path.insert(0, parent)` — fewer duplicates, one source of truth. **Recommended for implementation.**

---

## MCP tool design (Phase 1)

### Tool: `translate_pdf_base64`

**Input (JSON schema):**

```json
{
  "pdf_base64": "string (required) — base64-encoded PDF bytes",
  "target_lang": "string (required) — ISO code, e.g. en, hi, ar",
  "source_lang": "string (optional) — hint only, not used by extract"
}
```

**Output (success):**

```json
{
  "ok": true,
  "pdf_base64": "string — translated PDF, base64",
  "stats": {
    "pages": 50,
    "strings_translated": 852,
    "shards": 15,
    "timing_s": {
      "extract": 2.1,
      "translate": 45.4,
      "check": 0.3,
      "apply": 38.2,
      "total": 86.0
    },
    "input_bytes": 1138874,
    "output_bytes": 1200000
  }
}
```

**Output (failure):**

```json
{
  "ok": false,
  "error": "human-readable",
  "stage": "extract | translate | check | apply",
  "exit_code": 3
}
```

**Server-side steps (`pipeline.py`):**

1. `uuid` temp dir under `pdf-translate-mcp/runs/<job_id>/`
2. `base64.b64decode(pdf_base64)` → `input.pdf`
3. `subprocess` or `import` **extract.py** → `work/`
4. **translate** — call logic from `translate_poc.py` (async, parallel shards, Anthropic API key from env)
5. **check.py** `work/` — abort if exit ≠ 0
6. **apply.py** `work/ output.pdf --target <lang>`
7. Read `output.pdf` → `base64.b64encode` → return
8. Optional: delete temp dir after success (or keep for debug with `KEEP_RUNS=1`)

**Claude’s job after tool returns:**

- Decode `pdf_base64` and write to user-requested path (or user downloads from a path if we switch to Phase 2).

---

## MCP tool design (Phase 2 — fallback, not Phase 1)

| Tool | Purpose |
|------|---------|
| `translate_pdf_file` | Args: `input_path`, `output_path`, `target_lang` — no base64 |
| `translate_pdf_job_start` | Returns `job_id` immediately |
| `translate_pdf_job_status` | Poll until done; returns path or base64 chunk |

Implement Phase 2 only if Phase 1 hits size/timeout limits.

---

## Dependencies

```
mcp>=1.0.0              # Model Context Protocol Python SDK
anthropic>=0.40.0       # translate step (from translate_poc)
pymupdf>=1.23.0         # extract + apply (scripts auto-install too)
```

Scripts may also pull `fonttools`, `arabic-reshaper`, `python-bidi` on first apply.

**Secrets:**

- `ANTHROPIC_API_KEY` in environment (MCP server inherits from Claude Desktop config `env` block — **not** in tool args).

---

## How Claude connects (Claude Desktop — primary host)

You run the MCP server **locally**. Claude Desktop spawns it as a child process and bridges tool calls — same role Cursor would play, but you chat in the **Claude Desktop app**, not the IDE.

### 1. Config file location (Windows)

```
%APPDATA%\Claude\claude_desktop_config.json
```

Full path example:

```
C:\Users\neelima\AppData\Roaming\Claude\claude_desktop_config.json
```

Create the file if it does not exist. If you already have other MCP servers, **merge** into the existing `mcpServers` object — do not overwrite the whole file.

### 2. Example config

Use the full path to `python.exe` on Windows (more reliable than `python` alone):

```json
{
  "mcpServers": {
    "pdf-translate": {
      "command": "C:/Users/neelima/anaconda3/python.exe",
      "args": ["D:/Internship/skill/pdf-translate-mcp/server.py"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Find your Python: `where python` in PowerShell, or use the conda env you use for this project.

### 3. Restart and verify

1. **Quit Claude Desktop completely** (tray icon → Exit), then reopen.
2. Start a **new chat**.
3. Look for a **tools / hammer** icon or “pdf-translate” in connected integrations — exact UI varies by version.
4. Ask: *“What MCP tools do you have?”* — Claude should list `translate_pdf_base64` (after we implement `server.py`).

### 4. Two different “Claude” uses in this setup

| Who calls what | Pays for what |
|----------------|---------------|
| **Claude Desktop chat** | Your Claude **subscription** (Pro/Max) — orchestrates the conversation and decides to call the tool |
| **MCP server → Anthropic API** | Your **API key** inside `translate_poc` — parallel shard translation (~45s) |

The chat model does not run extract/apply; your local server does. The API key is still required for the **translate** step inside the tool unless you change the design to have Claude translate shards manually (not recommended).

### 5. Claude Desktop vs Cursor

Same MCP protocol and same `server.py`. Only the config file path differs:

| Host | Config file |
|------|-------------|
| **Claude Desktop** | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor (optional) | `.cursor/mcp.json` in project or user home |

This project targets **Claude Desktop only**.

---

## Implementation phases

### Phase 0 — Local pipeline (no MCP) ✓ validate first

- `tests/test_pipeline_local.py`: read `50_pages_french.pdf` → run `pipeline.run()` → write `out.pdf`
- Confirms scripts + translate work when orchestrated by Python
- **Gate:** must pass before MCP wiring

### Phase 1 — MCP server (your ask)

- `server.py` + `pipeline.py` + `config.py`
- Single tool `translate_pdf_base64`
- README with Claude Desktop config

### Phase 1b — Measure limits

- Log argument size, response size, wall-clock
- Document: did Claude.ai accept the call? timeout? truncation?

### Phase 2 — Production-shaped API (if needed)

- Path-based or job-based tools
- Optional: split `translate_shard` tool so Claude translates (unlikely — server should use API like POC)

---

## Translation: who translates?

| Option | Pros | Cons |
|--------|------|------|
| **A. Server calls Anthropic API** (`translate_poc` logic) | Fully automated; matches POC timings | Needs API key on server |
| **B. Claude translates shards via separate MCP tools** | No API key on server | Many tool calls; Claude still reads shard JSON (OK) but slow |

**Recommendation:** **Option A** inside the monolithic tool — same as your `translate_poc.py`. The whole point of the MCP test is server does everything; Claude only passes PDF in and gets PDF out.

---

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Tool call timeout (~90s+) | Increase client timeout if possible; or Phase 2 async job |
| 1.5MB arg too large | Phase 2 file path; or chunk upload tool |
| 1.5MB response truncated | Return `output_path` only; never base64 back |
| Windows: apply.py no fork | Serial apply still works (~40s); documented |
| API rate limits | Reuse stall-retry from translate_poc if needed |

---

## What we reuse (no rewrite)

| File | Role |
|------|------|
| `extract.py` | Stage 1 — subprocess `python extract.py input.pdf work/` |
| `translate_poc.py` | Stage 2 — import `main_async` or subprocess |
| `check.py` | Stage 3 — subprocess, gate on exit 0 |
| `apply.py` | Stage 4 — subprocess with `--target` |

`pipeline.py` is the only new orchestration layer (~80–120 lines).

---

## Success criteria for the experiment

1. **Local:** `test_pipeline_local.py` produces valid translated PDF from your 50-page French file.
2. **MCP:** Claude Desktop can call `translate_pdf_base64` and receive a decodable PDF.
3. **Metrics logged:** total time, sizes in/out, whether Claude context choked.
4. **Decision:** proceed with base64 for Claude.ai, or pivot to path/job API.

---

## Open decisions (confirm before implementation)

1. **Symlink vs `sys.path` to parent scripts?** → Recommend `sys.path` to `d:\Internship\skill\`.
2. **Delete temp runs after success?** → Default yes; `KEEP_RUNS=1` for debug.
3. **API key:** env only (not in repo).
4. **Implement Phase 1 only first?** → Yes, unless you want path-based tool in the same PR.

---

## Next step

Reply with:

- Approve Phase 0 + Phase 1 as described, or
- Skip base64 test and go straight to **path-based** MCP tool, or
- Any change to tool name / return shape

Then implementation: create `pdf-translate-mcp/` files per layout above.

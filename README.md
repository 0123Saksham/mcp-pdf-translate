# pdf-translate MCP

Local MCP server for Claude Desktop that runs the full **pdf-translate** pipeline:

`extract.py` → parallel API translation → `check.py` → `apply.py`

## Install

```powershell
cd D:\Internship\skill\pdf-translate-mcp
pip install -r requirements.txt
```

Parent scripts (`extract.py`, `check.py`, `apply.py`) live in `D:\Internship\skill\` and are called automatically.

## Claude Desktop config

Edit `%APPDATA%\Claude\claude_desktop_config.json` — **merge** into existing `mcpServers`:

```json
"pdf-translate": {
  "command": "C:\\Users\\neelima\\Anaconda3\\python.exe",
  "args": ["D:\\Internship\\skill\\pdf-translate-mcp\\server.py"],
  "env": {
    "ANTHROPIC_API_KEY": "sk-ant-YOUR_KEY_HERE"
  }
}
```

Fully quit Claude Desktop (tray → Exit), reopen, start a new chat.

## Tools

| Tool | When to use |
|------|-------------|
| `start_translate_pdf_job` | **Recommended for 50-page PDFs.** Returns `job_id` in &lt;1s; poll with `get_translate_pdf_job`. |
| `get_translate_pdf_job` | Poll every 15–30s until `status` is `done` or `failed`. |
| `translate_pdf_path` | Small/fast one-shot jobs only (Desktop kills tools after ~4 min). |
| `translate_pdf_base64` | Tiny PDFs only (~&lt;750KB raw). |

### Example prompt (job-based — use this for 50 pages)

> Call `start_translate_pdf_job` on `C:\Users\neelima\Downloads\50_pages_french.pdf` with `target_lang` en and `output_path` `C:\Users\neelima\Downloads\50_pages_french_en.pdf`. Poll `get_translate_pdf_job` every 20 seconds until done. Report `stats.timing_s` and wall-clock time.

### Example prompt (blocking path — small PDFs only)

> Call `translate_pdf_path` on `C:\Users\neelima\Downloads\small.pdf` with `target_lang` en.

### Base64 limit

Do **not** use `translate_pdf_base64` for multi-page PDFs in Desktop. `read_media_file` and tool results are capped at **1MB**; a 1.09 MB PDF becomes ~1.5M chars of base64 and fails before translation starts.

## Local test (no Claude)

```powershell
# Extract only (no API key)
python tests\test_pipeline_local.py --extract-only "C:\Users\neelima\Downloads\50_pages_french.pdf"

# Full pipeline (needs ANTHROPIC_API_KEY)
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python tests\test_pipeline_local.py "C:\Users\neelima\Downloads\50_pages_french.pdf" -t en
```

Job artifacts: `pdf-translate-mcp/runs/<job_id>/`

## Timing (typical 50-page doc)

| Stage | ~seconds |
|-------|----------|
| extract | 2–5 |
| translate | 45–60 |
| check | <1 |
| apply | 35–45 |
| **total** | **~85–110** |

## Troubleshooting

### “Linux cloud VM” / paths not reachable / no `translate_pdf_path`

You are almost certainly in **Cowork** mode, not a **local MCP chat**.

| Mode | Where it runs | `translate_pdf_path` | `C:\Users\...` paths |
|------|---------------|----------------------|----------------------|
| **Regular Desktop chat** | Your PC | Yes (if MCP connected) | Yes |
| **Cowork** | Remote Linux VM | No | No |

**Fix:** In Claude Desktop, start a **normal new chat** (not Cowork). Cowork uses the pdf-translate **skill** in a VM; your MCP server only works in regular chat.

### pdf-translate tools not listed

1. **Quit Desktop fully** (tray → Exit) after editing `claude_desktop_config.json`.
2. Reopen → **new regular chat**.
3. Check logs: `%APPDATA%\Claude\logs\` for `mcp-server-pdf-translate.log` or `[pdf-translate]` in `mcp.log`.
4. If only `filesystem` appears in logs, Desktop never started `pdf-translate` — fix JSON syntax or Python path.

### Server not listed
- **Connection error:** Run `python server.py` manually — should hang waiting on stdin (that’s normal).
- **ANTHROPIC_API_KEY:** Must be in the `env` block of Desktop config.

### 4-minute timeout

Claude Desktop **hard-kills any MCP tool call after ~4 minutes**, even if the server is still working. Your 08:52 run proves this: the server was still translating when Desktop cancelled.

Use **`start_translate_pdf_job`** + **`get_translate_pdf_job`** for 50-page PDFs. The start call returns in under a second; poll until `status` is `done`.

If the MCP log shows `asyncio.run() cannot be called from a running event loop`, restart Claude Desktop after updating the server.

### Large base64 fails
Use `translate_pdf_path` instead. Desktop caps tool payloads at ~1MB.

See `PLAN.md` for architecture notes.

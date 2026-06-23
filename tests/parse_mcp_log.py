import re
from pathlib import Path

text = Path(r"C:\Users\neelima\AppData\Roaming\Claude\logs\mcp-server-pdf_translate.log").read_text(
    encoding="utf-8", errors="replace"
)
for line in reversed(text.splitlines()):
    if "Message from server" in line and '"tools":[' in line:
        names = re.findall(r'"name":"([^"]+)"', line)
        print("Last tools/list:", names)
        break

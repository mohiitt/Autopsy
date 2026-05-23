"""Self-contained fallback dashboard.

Loads HTML and JS from sibling files at import time so the FastAPI app can
serve them when no React build is present.
"""
from pathlib import Path

_HERE = Path(__file__).parent

try:
    FALLBACK_HTML = (_HERE / "_dashboard.html").read_text(encoding="utf-8")
except Exception:
    FALLBACK_HTML = (
        "<html><body><h1>autopsy</h1>"
        "<p>Fallback dashboard could not be loaded. "
        "Hit <code>/api/sessions</code> to see traces.</p></body></html>"
    )

try:
    FALLBACK_JS = "\n".join([
        (_HERE / "_dashboard_part1.js").read_text(encoding="utf-8"),
        (_HERE / "_dashboard_part2.js").read_text(encoding="utf-8"),
        (_HERE / "_dashboard_part3.js").read_text(encoding="utf-8"),
    ])
except Exception:
    FALLBACK_JS = "/* autopsy fallback js missing */"

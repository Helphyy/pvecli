"""Draw the Mermaid diagrams of the built site once, at build time.

The theme draws each diagram in the browser after the page has loaded, so
diagrams popped in a moment after every click. This opens every built page
that has one in a headless Chromium, lets the theme draw it exactly as it
would for a reader, and writes the drawing into the page as a declarative
shadow root: the same isolation the theme uses, so its colors still follow
the light and dark schemes through the --md-mermaid-* variables.

    python -m scripts.docs.prerender_diagrams [SITE_DIR]

Run after `zensical build`. Needs the `docs` extra and
`python -m playwright install chromium`.
"""

from __future__ import annotations

import functools
import http.server
import re
import sys
import threading
from pathlib import Path

DIAGRAM = re.compile(r'<pre class="mermaid">.*?</pre>', re.S)
# The pinned Mermaid of overrides/main.html: useless once drawn
MERMAID_TAGS = re.compile(r'[ \t]*<(?:link rel="preload"|script) [^>]*mermaid@[^>]*>(?:</script>)?\n?')

# Open the theme's closed shadow roots, so the drawing can be read
OPEN_SHADOWS = """
const attach = Element.prototype.attachShadow;
Element.prototype.attachShadow = function (init) {
  return attach.call(this, { ...init, mode: "open" });
};
"""


def inline(html: str, drawings: list[str]) -> str:
    """Put each drawing in place of its source, in page order."""
    blocks = DIAGRAM.findall(html)
    if len(blocks) != len(drawings):
        raise ValueError(f"{len(blocks)} diagrams in the page, {len(drawings)} drawn")
    it = iter(drawings)
    html = DIAGRAM.sub(
        lambda _: f'<div class="mermaid"><template shadowrootmode="open">{next(it)}</template></div>',
        html,
    )
    return MERMAID_TAGS.sub("", html)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def prerender(site: Path) -> int:
    from playwright.sync_api import sync_playwright

    pages = [p for p in sorted(site.rglob("*.html")) if '<pre class="mermaid">' in p.read_text()]
    server, base = _serve(site)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.add_init_script(OPEN_SHADOWS)
            for path in pages:
                url = base + path.relative_to(site).as_posix()
                page.goto(url, wait_until="load")
                # The sketch font must be there before Mermaid measures the labels
                page.evaluate("document.fonts.ready")
                page.wait_for_function("!document.querySelector('pre.mermaid')", timeout=30000)
                drawings = page.evaluate(
                    "[...document.querySelectorAll('div.mermaid')].map(d => d.shadowRoot.innerHTML)"
                )
                path.write_text(inline(path.read_text(), drawings))
                print(f"{path.relative_to(site)}: {len(drawings)} diagram(s)")
            browser.close()
    finally:
        server.shutdown()
    return len(pages)


if __name__ == "__main__":
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
    print(f"{prerender(site)} page(s) drawn")

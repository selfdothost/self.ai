#!/usr/bin/env python3
"""Smoke-test the Firecrawl (self.crawl) connection the knowledge base uses.

Runs the same firecrawl-py v2 call path as the web loader
(``SafeFirecrawlLoader``: ``scrape`` -> markdown), so a green run means the
web-search loader and "crawl a site into a knowledge base" path will work
against this endpoint.

Run it inside the api image / venv (where ``firecrawl-py`` is installed) the
moment self.crawl comes online:

    FIRECRAWL_API_BASE_URL=http://<self-crawl-endpoint>:3002 \
    [FIRECRAWL_API_KEY=...] \
        python scripts/verify-firecrawl.py [URL]

Exits 0 on success, non-zero on failure. No hostnames are baked in — the
endpoint always comes from the environment.
"""
import os
import sys


def main() -> int:
    base = os.environ.get("FIRECRAWL_API_BASE_URL", "").strip()
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip() or "no-key"
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"

    if not base:
        print(
            "FAIL: set FIRECRAWL_API_BASE_URL to your self.crawl endpoint.",
            file=sys.stderr,
        )
        return 2

    try:
        from firecrawl import FirecrawlApp
    except ImportError:
        print(
            "FAIL: firecrawl-py not installed — run inside the api image/venv.",
            file=sys.stderr,
        )
        return 2

    print(f"Firecrawl endpoint : {base}")
    print(f"Test URL           : {url}")

    try:
        app = FirecrawlApp(api_key=key, api_url=base)
        result = app.scrape(url, formats=["markdown", "html"])
    except Exception as e:  # noqa: BLE001 — surface any SDK/transport failure
        print(f"FAIL: scrape errored ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    markdown = getattr(result, "markdown", None) or (
        result.get("markdown", "") if isinstance(result, dict) else ""
    )
    n = len(markdown or "")
    if n == 0:
        print(
            "FAIL: reached the endpoint but got 0 markdown — check the crawler "
            "workers / that the URL is fetchable.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: scraped {n} chars of markdown. Firecrawl connection verified.")
    print(
        "Enable it in the webui with RAG_WEB_LOADER_ENGINE=firecrawl + "
        "FIRECRAWL_API_BASE_URL/KEY (an embeddings endpoint must also be set — "
        "crawled pages are embedded)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

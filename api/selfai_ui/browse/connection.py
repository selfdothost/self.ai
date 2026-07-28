"""Core Playwright connection (cavekit-browse-connection.md).

self.ai's own, first-class, independently-deployable connection to a
Playwright browser-automation service — not proxied through the
domain-crawl backend (self.crawl / Firecrawl). Implements R1 (mutual
authentication), R2 (the fetch request/response contract), R3
(script/tracker stripping), R4 (SSRF protection — literal IP and hostname
resolution), R5 (allow/blocklist enforcement), and R6 (tuning enforcement).

browse_fetch() takes an already-resolved BrowseProfile (see
cavekit-browse-profiles.md for how a profile gets resolved by name) rather
than resolving one itself — callers own resolution, this module only
enforces what a resolved profile says.

Scope note on R1's first acceptance criterion ("a request without valid
credentials is rejected by the Playwright service"): that is a property of
whatever Playwright service is deployed, not something self.ai's client can
enforce from here. What this module guarantees on self.ai's side: every
outbound request carries credentials sourced from configuration (never a
literal in source), and a service response indicating the credentials were
rejected (401/403) is surfaced as a clear, distinguishable failure — which
also directly serves R2's "failure must never look like empty success"
requirement.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from html import unescape
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from fastapi import Request
from pydantic import BaseModel

from selfai_ui.browse.profiles import BrowseProfile

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

# R3: strip embedded <script> elements before content ever reaches a caller.
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SELF_CLOSING_SCRIPT_RE = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)

# R3: known tracker resource patterns. Matches the whole tag referencing a
# known tracking/analytics host, on any tag (script/img/iframe all get used
# for tracking pixels/beacons). Not exhaustive — a starting, testable set.
_TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.com/tr",
    "doubleclick.net",
    "connect.facebook.net",
    "hotjar.com",
    "segment.io",
    "mixpanel.com",
)
_TRACKER_HOST_ALTERNATION = "|".join(re.escape(h) for h in _TRACKER_HOSTS)
_TRACKER_TAG_RE = re.compile(
    r"<[a-zA-Z]+\b[^>]*\b(?:src|href)=[\"'][^\"']*(?:" + _TRACKER_HOST_ALTERNATION + r")[^\"']*[\"'][^>]*/?>",
    re.IGNORECASE,
)


def strip_scripts_and_trackers(html: str) -> str:
    """R3: return `html` with embedded <script> elements and known tracker
    resource tags removed."""
    html = _SCRIPT_TAG_RE.sub("", html)
    html = _SELF_CLOSING_SCRIPT_RE.sub("", html)
    html = _TRACKER_TAG_RE.sub("", html)
    return html


_ANY_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plaintext(html: str) -> str:
    """Reduce fetched markup to plain text.

    For a text/plain resource fetched through the browser (robots.txt is the
    motivating case), Chromium wraps the body in `<pre>…</pre>`, so the
    original text survives verbatim once tags are removed and entities are
    unescaped. Scripts are dropped first so their contents never leak into the
    text. This is deliberately simple — it is for machine-readable text files,
    not for extracting readable prose from a rich page (that is html_to_text's
    job over in the search path)."""
    html = _SCRIPT_TAG_RE.sub("", html)
    html = _SELF_CLOSING_SCRIPT_RE.sub("", html)
    html = _ANY_TAG_RE.sub("", html)
    return unescape(html)


def _hostname_matches_list(hostname: str, patterns: list[str]) -> bool:
    """R5: True if `hostname` matches any entry in `patterns` — an exact
    match, or a subdomain of the pattern (so "example.com" in a list also
    covers "www.example.com")."""
    hostname = hostname.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if hostname == pattern or hostname.endswith("." + pattern):
            return True
    return False


def _is_blocked_literal_ip_host(hostname: str) -> bool:
    """R4 (literal-IP half): True if `hostname` is itself a literal IP
    address in a private (RFC1918), loopback, or link-local range."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False  # Not a literal IP — nothing for this check to do.
    return ip.is_private or ip.is_loopback or ip.is_link_local


async def _hostname_resolves_to_blocked_ip(hostname: str) -> bool:
    """R4 (hostname-resolution half): True if `hostname` resolves to *any*
    private, loopback, or link-local address. Checks every resolved
    address, not just the first, since a hostname can carry multiple
    A/AAAA records.

    Known limitation: this is a resolve-then-check, not resolve-and-pin —
    a genuinely adversarial DNS server could in principle answer
    differently between this check and the actual connect (DNS rebinding).
    Closing that gap requires pinning the resolved address across both
    steps, which is a deeper change than this requirement asks for; not
    silently ignored, just out of scope here.
    """
    try:
        loop = asyncio.get_running_loop()
        addrinfo = await loop.getaddrinfo(hostname, None)
    except (OSError, socket.gaierror):
        # Resolution failure isn't an SSRF concern — the actual fetch
        # attempt will surface its own clear network-failure result.
        return False

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    return False


class BrowseFetchResult(BaseModel):
    """R2: an explicit, never-silently-empty success/failure result."""

    success: bool
    content: Optional[str] = None
    error: Optional[str] = None


async def browse_fetch(
    request: Request, url: str, profile: BrowseProfile, raw_text: bool = False
) -> BrowseFetchResult:
    """R2: fetch `url` through the core Playwright connection under `profile`.

    `profile` is expected to already be resolved (see
    cavekit-browse-profiles.md) — this function does not look profiles up by
    name itself.

    `raw_text=True` returns the fetched resource as plain text (tags stripped,
    entities unescaped) instead of tracker-stripped HTML. It exists for
    machine-readable text files fetched through the same tunnelled connection
    as pages — robots.txt above all — so the SSRF guard, allow/blocklist, auth,
    and tuning all apply identically and the yard's own address never reaches
    the origin. It is not a "give me the page as text" convenience for search;
    that path wants html_to_text's structure-aware extraction, not this.
    """
    service_url = request.app.state.config.BROWSE_PLAYWRIGHT_SERVICE_URL
    api_key = request.app.state.config.BROWSE_PLAYWRIGHT_API_KEY

    if not service_url:
        return BrowseFetchResult(
            success=False,
            error="No Playwright service configured (BROWSE_PLAYWRIGHT_SERVICE_URL)",
        )

    # R4: refuse before any network fetch is attempted — both the literal-IP
    # case and the resolves-to-a-blocked-address case.
    hostname = urlparse(url).hostname
    if hostname:
        if _is_blocked_literal_ip_host(hostname):
            return BrowseFetchResult(
                success=False,
                error=f"Refused: {hostname} is a private, loopback, or link-local address",
            )
        if await _hostname_resolves_to_blocked_ip(hostname):
            return BrowseFetchResult(
                success=False,
                error=f"Refused: {hostname} resolves to a private, loopback, or link-local address",
            )

    # R5: enforce the resolved profile's allow/blocklist before any fetch.
    # Blocklist wins even if a URL also happens to satisfy the allowlist —
    # it's the more specific override.
    if hostname and profile.blocklist and _hostname_matches_list(hostname, profile.blocklist):
        return BrowseFetchResult(
            success=False,
            error=f"Refused: {hostname} matches profile {profile.name!r}'s blocklist",
        )
    if hostname and profile.allowlist and not _hostname_matches_list(hostname, profile.allowlist):
        return BrowseFetchResult(
            success=False,
            error=f"Refused: {hostname} is not in profile {profile.name!r}'s allowlist",
        )

    # R6: tuning — the resolved profile's timeout and retry count, not a
    # default or another profile's values.
    timeout_seconds = profile.timeout_seconds if profile and profile.timeout_seconds else DEFAULT_TIMEOUT_SECONDS
    retry_count = profile.retry_count if profile else 0

    # The Playwright service's own page-load timeout defaults to 15s
    # (selftools/playwright) unless told otherwise. Without this, a
    # profile's timeout_seconds only bounds our client wait — it never
    # actually controls how long Playwright itself spends navigating,
    # so a longer client timeout couldn't help a page that Playwright's
    # own default would already give up on first. Leave a margin below
    # our own client timeout for the response to actually come back.
    playwright_timeout_ms = max(1000, int((timeout_seconds - 3) * 1000))

    headers = {"Content-Type": "application/json"}
    if api_key:
        # R1: credentials sourced from configuration, never a literal in
        # application code.
        headers["Authorization"] = f"Bearer {api_key}"

    last_result: Optional[BrowseFetchResult] = None

    for attempt in range(retry_count + 1):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as session:
                async with session.post(
                    f"{service_url.rstrip('/')}/scrape",
                    json={"url": url, "timeout": playwright_timeout_ms},
                    headers=headers,
                ) as response:
                    if response.status in (401, 403):
                        # R1: the service rejected our credentials (or lack
                        # thereof) — a clear, distinguishable failure, not a
                        # silent empty success. Not worth retrying — bad
                        # credentials won't fix themselves.
                        return BrowseFetchResult(
                            success=False,
                            error=f"Playwright service rejected credentials (HTTP {response.status})",
                        )

                    if response.status != 200:
                        body = await response.text()
                        last_result = BrowseFetchResult(
                            success=False,
                            error=f"Playwright service returned HTTP {response.status}: {body[:500]}",
                        )
                        continue  # transient-ish — worth a retry, per profile.retry_count

                    data = await response.json()
                    content = data.get("content")

                    if not content:
                        # R2 AC3: no usable content is a failure, never a
                        # silently empty success.
                        last_result = BrowseFetchResult(success=False, error="Fetch returned no usable content")
                        continue

                    content = html_to_plaintext(content) if raw_text else strip_scripts_and_trackers(content)

                    if not content.strip():
                        # Processing left nothing usable — still a failure, not
                        # a silent empty success.
                        last_result = BrowseFetchResult(success=False, error="Fetch returned no usable content")
                        continue

                    return BrowseFetchResult(success=True, content=content)

        except Exception as e:
            # R2: this function must never raise — every failure mode
            # (network error, timeout, malformed response body, anything
            # else) becomes a clear BrowseFetchResult instead. This matters
            # more now that callers may run several browse_fetch() calls
            # concurrently via asyncio.gather() (cavekit-browse-search
            # -migration.md R2) — an uncaught exception here would abort
            # every other in-flight fetch, not just this one.
            log.warning(f"browse_fetch attempt {attempt + 1}/{retry_count + 1} failed for {url!r}: {e}")
            last_result = BrowseFetchResult(success=False, error=f"Fetch failed: {e}")
            continue

    return last_result

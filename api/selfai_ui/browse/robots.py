"""robots.txt honoring for link-following traversal
(cavekit-browse-web-access.md — the deferred crawler-etiquette item).

deep_research follows links across a site, which is crawler behavior, so it
honors robots.txt: a URL a site disallows for our agent is not fetched, and a
Crawl-delay a site declares is respected between fetches to that origin. This
is politeness on its own merit, not reputational self-protection — fetches
leave over the Playwright tunnel and never carry the yard's address, so nothing
here is about avoiding a ban. We simply do not want to be the kind of crawler
that ignores the file.

web_fetch (one page a user named) is deliberately NOT gated by this. Reading a
single page a human asked for is ordinary user-agent behavior, not crawling.

The robots.txt itself is fetched through the same tunnelled connection as
pages (connection.browse_fetch raw_text=True) — never a direct fetch from the
API pod — so the check runs from the same network vantage as the fetch it
gates, and the yard's IP still never reaches the origin.
"""

import asyncio
import logging
from typing import Optional
from urllib import robotparser
from urllib.parse import urlparse, urlunparse

from selfai_ui.browse.connection import browse_fetch
from selfai_ui.browse.profiles import BrowseProfile

log = logging.getLogger(__name__)


def _origin_key(url: str) -> Optional[tuple[str, str]]:
    """(scheme, netloc) for `url`, or None if it has no host. robots.txt is
    per-origin, so this is the cache key and the thing a robots.txt governs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return (parsed.scheme, parsed.netloc)


def _robots_url(scheme: str, netloc: str) -> str:
    return urlunparse((scheme, netloc, "/robots.txt", "", "", ""))


class RobotsCache:
    """Per-traversal robots.txt cache.

    One instance lives for the duration of a single deep_research call: it
    fetches and parses each origin's robots.txt at most once, and serializes
    concurrent first-touches of the same origin so a batch of same-site URLs
    does not fetch robots.txt several times over.

    A missing (404) or unreachable robots.txt is treated as "allow all" — the
    standard convention, and the safe-for-availability one: a robots fetch
    hiccup must not silently forbid an entire site. A robots.txt that is present
    but does not mention our agent falls through to its ``*`` rules, exactly as
    RobotFileParser implements.
    """

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        # origin -> parsed RobotFileParser (or None once fetched-and-empty/allow-all)
        self._parsers: dict[tuple[str, str], Optional[robotparser.RobotFileParser]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def _parser_for(
        self, request, origin: tuple[str, str], profile: BrowseProfile
    ) -> Optional[robotparser.RobotFileParser]:
        if origin in self._parsers:
            return self._parsers[origin]

        # Serialize first-touch per origin so N concurrent same-site URLs share
        # one fetch rather than racing N fetches of the same robots.txt.
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._parsers:
                return self._parsers[origin]

            scheme, netloc = origin
            result = await browse_fetch(request, _robots_url(scheme, netloc), profile, raw_text=True)

            parser: Optional[robotparser.RobotFileParser] = None
            if result.success and result.content and result.content.strip():
                parser = robotparser.RobotFileParser()
                try:
                    parser.parse(result.content.splitlines())
                except Exception as e:
                    # A malformed robots.txt should not fail the traversal.
                    # Treat it as absent (allow-all) rather than guessing.
                    log.warning(f"robots.txt parse error for {netloc}: {e}")
                    parser = None
            else:
                log.debug(f"robots.txt for {netloc}: none/unreachable, treating as allow-all")

            self._parsers[origin] = parser
            return parser

    async def is_allowed(self, request, url: str, profile: BrowseProfile) -> bool:
        """True if our agent may fetch `url` under the origin's robots.txt.
        Absent/unreachable/malformed robots.txt → allowed."""
        origin = _origin_key(url)
        if origin is None:
            return False  # no host to reason about — never fetched anyway
        parser = await self._parser_for(request, origin, profile)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def max_crawl_delay(self, request, urls: list[str], profile: BrowseProfile) -> Optional[float]:
        """The largest Crawl-delay any of `urls`' origins declares for our
        agent, or None if none declare one. Used to derive a single
        inter-request delay for a crawl that starts from one or more URLs."""
        delays = [d for d in [await self.crawl_delay(request, u, profile) for u in urls] if d is not None]
        return max(delays) if delays else None

    async def crawl_delay(self, request, url: str, profile: BrowseProfile) -> Optional[float]:
        """The Crawl-delay (seconds) the origin declares for our agent, or None
        if it declares none. Reads the ``*`` group when no agent-specific group
        matches, matching RobotFileParser's own resolution."""
        origin = _origin_key(url)
        if origin is None:
            return None
        parser = await self._parser_for(request, origin, profile)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:
            return None
        return float(delay) if delay is not None else None

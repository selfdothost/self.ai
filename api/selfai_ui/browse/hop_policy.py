"""Cross-origin hop policy for link-following traversal
(cavekit-browse-web-access.md R6).

Where a URL came from decides whether it may be followed across origins:

- A URL the **search provider** returned may be read whatever its origin. It
  did not come from any page's content, so no fetched page chose it.
- A URL discovered **in fetched page content** may only be followed when it
  stays on the same site as the page it was found on.

That second rule is the structural break in the prompt-injection chain. A page
can contain text addressed to the model, and that text can name the next URL —
so without this rule, an injected page could hand the model an attacker-owned
destination and let a query string carry conversation contents out. With it, a
compromised page can send the traversal deeper into its own site and nowhere
else.

## What "same site" means here, and why it is deliberately stricter than
## "same registrable domain"

The kit states the rule as *same registrable domain*. Computing that correctly
requires the Public Suffix List: `example.co.uk` and `example.com` are
registrable domains, but `co.uk` is not, so you cannot simply take the last two
labels. This codebase has no PSL library, and adding one has a failure mode
worth naming: a bundled suffix snapshot ages, and when a new multi-label suffix
appears that the snapshot lacks, two genuinely different sites start being
judged same-site. That is a security property that silently decays.

So this implements a stricter rule that needs no suffix data at all: a hop is
allowed when the two hosts are equal, or when one is a subdomain of the other.

    example.com          -> example.com/other        allowed
    example.com          -> docs.example.com         allowed (subdomain)
    www.example.com      -> example.com/x            allowed (parent)
    www.example.com      -> docs.example.com         REFUSED (siblings)
    foo.co.uk            -> bar.co.uk                REFUSED
    example.com          -> attacker.example         REFUSED

This never permits a hop that "same registrable domain" would forbid — it is a
strict subset — so it satisfies R6's "only when" criterion. It gives up sibling
subdomains when the starting page is itself on a subdomain, which is a real
usability cost on sites that split content across `www.` and `docs.`. That
trade is taken deliberately: this rule over-blocks, and the PSL-based one
under-blocks as its data ages. On a security boundary, prefer the failure that
refuses.

Note also what this is not: it is not a substitute for the SSRF guard in
connection.py (R4), which still runs on every fetch. This decides *whether a
discovered link is eligible to be followed at all*, before that guard is ever
reached.
"""

from typing import Optional
from urllib.parse import urlparse


def host_of(url: str) -> Optional[str]:
    """The normalized hostname of `url`, or None if it has none.

    Lowercased, and a trailing root dot ("example.com.") is stripped — both are
    ways of writing the same host, and treating them as different would let a
    trivial rewrite bypass the comparison below.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        # Touch .port deliberately. `.hostname` does not validate the port, so
        # "https://example.com:bad/x" yields a perfectly ordinary-looking
        # hostname and would compare equal to the real example.com. `.port` is
        # what raises on a malformed authority, and a URL we cannot fully parse
        # is one we should refuse to reason about rather than half-trust.
        parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    return hostname.lower().rstrip(".")


def is_same_site(page_url: str, candidate_url: str) -> bool:
    """R6: True if `candidate_url` may be followed from `page_url` when the
    candidate was discovered in that page's content.

    Returns False whenever either host is missing or unparseable — an unknown
    origin is never treated as the same origin.
    """
    page_host = host_of(page_url)
    candidate_host = host_of(candidate_url)

    if not page_host or not candidate_host:
        return False

    if page_host == candidate_host:
        return True

    return candidate_host.endswith("." + page_host) or page_host.endswith("." + candidate_host)

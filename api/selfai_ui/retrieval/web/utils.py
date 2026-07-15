import logging
import socket
import urllib.parse
from typing import Iterator, Sequence, Union

import validators
from langchain_community.document_loaders import (
    WebBaseLoader,
)
from langchain_core.documents import Document

from selfai_ui.config import ENABLE_RAG_LOCAL_WEB_FETCH
from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


def validate_url(url: Union[str, Sequence[str]]):
    if isinstance(url, str):
        if isinstance(validators.url(url), validators.ValidationError):
            raise ValueError(ERROR_MESSAGES.INVALID_URL)
        if not ENABLE_RAG_LOCAL_WEB_FETCH:
            # Local web fetch is disabled, filter out any URLs that resolve to private IP addresses
            parsed_url = urllib.parse.urlparse(url)
            # Get IPv4 and IPv6 addresses
            ipv4_addresses, ipv6_addresses = resolve_hostname(parsed_url.hostname)
            # Check if any of the resolved addresses are private
            # This is technically still vulnerable to DNS rebinding attacks, as we don't control WebBaseLoader
            for ip in ipv4_addresses:
                if validators.ipv4(ip, private=True):
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)
            for ip in ipv6_addresses:
                if validators.ipv6(ip, private=True):
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)
        return True
    elif isinstance(url, Sequence):
        return all(validate_url(u) for u in url)
    else:
        return False


def resolve_hostname(hostname):
    # Get address information
    addr_info = socket.getaddrinfo(hostname, None)

    # Extract IP addresses from address information
    ipv4_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET]
    ipv6_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET6]

    return ipv4_addresses, ipv6_addresses


_MAX_REDIRECTS = 5


class SafeWebBaseLoader(WebBaseLoader):
    """WebBaseLoader with enhanced error handling and validated redirects.

    validate_url() only checks the URL the caller gave us. Plain
    requests.Session.get() (what the parent's _scrape() calls) follows
    redirects transparently by default -- so a URL that passes the initial
    check can still 302 to an internal target (cloud metadata, yard VIPs)
    and requests will fetch it without complaint. This overrides _scrape to
    disable automatic redirect-following and re-validate each hop through
    validate_url() before following it, bounded to _MAX_REDIRECTS.

    This does NOT close the DNS-rebinding gap noted in validate_url()'s
    docstring (same hostname resolving differently between the check here
    and the actual connect a moment later) -- that would need pinning the
    validated IP at the socket level, which requests/WebBaseLoader don't
    expose a clean hook for. self.crawl's own fetch engine (the default,
    non-local RAG_WEB_LOADER_ENGINE=firecrawl path) already does this
    properly at the connection level (see self.search/self.crawl/core's
    safeFetch.ts) -- prefer it over this direct-fetch path when possible.
    """

    def _scrape(self, url, parser=None, bs_kwargs=None):
        from bs4 import BeautifulSoup

        if parser is None:
            parser = "xml" if url.endswith(".xml") else self.default_parser
        self._check_parser(parser)

        current_url = url
        response = None
        for _ in range(_MAX_REDIRECTS):
            request_kwargs = {**self.requests_kwargs, "allow_redirects": False}
            response = self.session.get(current_url, **request_kwargs)
            if response.is_redirect or response.is_permanent_redirect:
                next_url = response.headers.get("Location")
                if not next_url:
                    break
                next_url = urllib.parse.urljoin(current_url, next_url)
                validate_url(next_url)  # raises ValueError on a private-IP target
                current_url = next_url
                continue
            break
        else:
            raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS}) while fetching {url}")

        if self.raise_for_status:
            response.raise_for_status()
        if self.encoding is not None:
            response.encoding = self.encoding
        elif self.autoset_encoding:
            response.encoding = response.apparent_encoding
        return BeautifulSoup(response.text, parser, **(bs_kwargs or {}))

    def lazy_load(self) -> Iterator[Document]:
        """Lazy load text from the url(s) in web_path with error handling."""
        for path in self.web_paths:
            try:
                soup = self._scrape(path, bs_kwargs=self.bs_kwargs)
                text = soup.get_text(**self.bs_get_text_kwargs)

                # Build metadata
                metadata = {"source": path}
                if title := soup.find("title"):
                    metadata["title"] = title.get_text()
                if description := soup.find("meta", attrs={"name": "description"}):
                    metadata["description"] = description.get("content", "No description found.")
                if html := soup.find("html"):
                    metadata["language"] = html.get("lang", "No language found.")

                yield Document(page_content=text, metadata=metadata)
            except Exception as e:
                # Log the error and continue with the next URL
                log.error(f"Error loading {path}: {e}")


def get_web_loader(
    urls: Union[str, Sequence[str]],
    verify_ssl: bool = True,
    requests_per_second: int = 2,
    engine: str = "",
    firecrawl_api_key: str = "",
    firecrawl_api_url: str = "",
):
    # Check if the URL is valid
    if not validate_url(urls):
        raise ValueError(ERROR_MESSAGES.INVALID_URL)

    if engine == "firecrawl":
        from selfai_ui.retrieval.web.firecrawl import SafeFirecrawlLoader

        log.info(f"Firecrawl loader: urls={urls!r} api_url={firecrawl_api_url!r}")
        return SafeFirecrawlLoader(urls, api_key=firecrawl_api_key, api_base_url=firecrawl_api_url or None)

    return SafeWebBaseLoader(
        urls,
        verify_ssl=verify_ssl,
        requests_per_second=requests_per_second,
        continue_on_failure=True,
    )

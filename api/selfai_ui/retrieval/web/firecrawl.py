from firecrawl import FirecrawlApp, AsyncFirecrawlApp
from firecrawl.v2.types import ScrapeOptions
from langchain_core.documents import Document as LCDocument
from typing import Union, Sequence, List, Optional, Callable
import logging
import asyncio
import json
import httpx

log = logging.getLogger(__name__)

class SafeFirecrawlLoader:
    def __init__(self, urls, api_key=None, api_base_url=None):
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self._api_key = api_key or "no-key"
        self._api_base_url = api_base_url
        app_kwargs = {"api_key": self._api_key}
        if api_base_url:
            app_kwargs["api_url"] = api_base_url
        self.app = FirecrawlApp(**app_kwargs)

    def load(self) -> List[LCDocument]:
        docs = []
        for url in self.urls:
            try:
                log.debug(f"Firecrawl: scraping {url}")
                result = self.app.scrape(url, formats=["markdown", "html"])
                markdown = getattr(result, "markdown", None) or (result.get("markdown", "") if isinstance(result, dict) else "")
                meta = getattr(result, "metadata", None) or (result.get("metadata", {}) if isinstance(result, dict) else {})
                title = (getattr(meta, "title", None) or (meta.get("title", "") if isinstance(meta, dict) else "")) or ""
                log.debug(f"Firecrawl: got result for {url} — markdown_len={len(markdown or '')}")
                docs.append(LCDocument(
                    page_content=markdown or "",
                    metadata={"source": url, "title": title},
                ))
            except Exception as e:
                log.error(f"Firecrawl error loading {url}: {e}", exc_info=True)
        log.info(f"Firecrawl load complete: {len(docs)}/{len(self.urls)} docs returned")
        return docs

    async def crawl(
        self,
        limit: int = 10,
        poll_interval: int = 2,
        timeout: int = 120,
    ) -> List[LCDocument]:
        """Async site crawl using AsyncFirecrawlApp. Follows links up to `limit` pages."""
        app_kwargs = {"api_key": self._api_key}
        if self._api_base_url:
            app_kwargs["api_url"] = self._api_base_url
        async_app = AsyncFirecrawlApp(**app_kwargs)

        docs = []
        for url in self.urls:
            try:
                log.debug(f"Firecrawl: starting crawl for {url} (limit={limit})")
                started = await async_app.start_crawl(url, limit=limit)
                crawl_id = started.id
                log.debug(f"Firecrawl: crawl job {crawl_id} started for {url}")

                elapsed = 0
                while elapsed < timeout:
                    try:
                        snapshot = await async_app.get_crawl_status(crawl_id)
                    except json.JSONDecodeError:
                        log.debug(f"Firecrawl crawl {crawl_id}: empty status response, retrying…")
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval
                        continue

                    status = snapshot.status
                    log.debug(f"Firecrawl crawl {crawl_id}: status={status} {snapshot.completed}/{snapshot.total}")

                    if status == "completed":
                        for page in snapshot.data:
                            meta = getattr(page, "metadata", None)
                            source = (getattr(meta, "source_url", None) or getattr(meta, "url", None) or url) if meta else url
                            markdown = getattr(page, "markdown", None) or ""
                            docs.append(LCDocument(
                                page_content=markdown,
                                metadata={
                                    "source": source,
                                    "title": getattr(meta, "title", "") or "" if meta else "",
                                },
                            ))
                        log.info(f"Firecrawl crawl {crawl_id}: completed, {len(snapshot.data)} pages")
                        break
                    elif status in ("failed", "cancelled"):
                        log.error(f"Firecrawl crawl {crawl_id}: {status} for {url}")
                        break

                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                else:
                    log.warning(f"Firecrawl crawl {crawl_id}: timed out after {timeout}s")

            except Exception as e:
                log.error(f"Firecrawl crawl error for {url}: {e}", exc_info=True)

        log.info(f"Firecrawl crawl complete: {len(docs)} docs from {len(self.urls)} url(s)")
        return docs

    def crawl_sync(self, limit: int = 10, poll_interval: int = 2, timeout: int = 120) -> List[LCDocument]:
        """Synchronous wrapper around crawl() for use in non-async callers."""
        return asyncio.run(self.crawl(limit=limit, poll_interval=poll_interval, timeout=timeout))

    def _extract_page(self, page, fallback_url: str):
        """Extract (markdown, source, title, status_code) from a raw dict or Document object."""
        if isinstance(page, dict):
            markdown = page.get("markdown") or ""
            meta = page.get("metadata") or {}
            source = (
                meta.get("sourceURL") or meta.get("source_url")
                or meta.get("url") or fallback_url
            )
            title = meta.get("title") or ""
            status_code = meta.get("statusCode")
        else:
            markdown = getattr(page, "markdown", None) or ""
            m = getattr(page, "metadata", None)
            source = (getattr(m, "source_url", None) or fallback_url) if m else fallback_url
            title = (getattr(m, "title", None) or "") if m else ""
            status_code = (getattr(m, "statusCode", None) or getattr(m, "status_code", None)) if m else None
        return markdown, source, title, status_code

    async def crawl_with_progress(
        self,
        job_state: dict,
        limit: int = 10,
        max_depth: Optional[int] = None,
        delay: Optional[int] = None,
        poll_interval: int = 2,
        max_consecutive_403s: Optional[int] = None,
        include_paths: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
        regex_on_full_url: Optional[bool] = None,
        crawl_entire_domain: Optional[bool] = None,
        resume_crawl_id: Optional[str] = None,
        resume_processed: int = 0,
        on_progress: Optional[Callable] = None,
        **_kwargs,  # absorb legacy timeout kwarg from old callers
    ) -> List[LCDocument]:
        """HTTP-polling crawl that appends pages to job_state["pages"] as they appear.

        The loop runs until Firecrawl reports completed/failed/cancelled, the user
        cancels the job, or a status poll times out (30 s httpx timeout) — whichever
        comes first.  There is no wall-clock timeout.

        The Firecrawl self-hosted API sometimes returns status="completed" with 0 pages
        immediately after start_crawl (before workers have begun).  We detect this
        "pending" state (completed == 0 and total == 0) and keep polling until the crawl
        genuinely has results.
        """
        app_kwargs = {"api_key": self._api_key}
        if self._api_base_url:
            app_kwargs["api_url"] = self._api_base_url
        async_app = AsyncFirecrawlApp(**app_kwargs)

        docs = []
        for url in self.urls:
            try:
                log.info(f"crawl_with_progress: starting crawl for {url} (limit={limit}, max_depth={max_depth}, delay={delay}s, include_paths={include_paths}, exclude_paths={exclude_paths}, regex_on_full_url={regex_on_full_url}, crawl_entire_domain={crawl_entire_domain})")
                scrape_options = ScrapeOptions(wait_for=delay * 1000) if delay else None
                crawl_kwargs: dict = {}
                # When crawling the entire domain, omit the page limit so that
                # include_paths/exclude_paths patterns are the sole constraint.
                if not crawl_entire_domain:
                    crawl_kwargs["limit"] = limit
                if scrape_options:
                    crawl_kwargs["scrape_options"] = scrape_options
                if max_depth is not None:
                    crawl_kwargs["max_depth"] = max_depth
                if include_paths:
                    crawl_kwargs["include_paths"] = include_paths
                if exclude_paths:
                    crawl_kwargs["exclude_paths"] = exclude_paths
                if regex_on_full_url is not None:
                    crawl_kwargs["regex_on_full_url"] = regex_on_full_url
                if crawl_entire_domain is not None:
                    crawl_kwargs["crawl_entire_domain"] = crawl_entire_domain
                if resume_crawl_id:
                    crawl_id = resume_crawl_id
                    processed = resume_processed
                    job_state["crawl_id"] = crawl_id
                    log.info(f"crawl_with_progress: resuming {crawl_id} from processed={processed}")
                else:
                    started = await async_app.start_crawl(url, **crawl_kwargs)
                    crawl_id = started.id
                    processed = 0
                    job_state["crawl_id"] = crawl_id
                    log.info(f"crawl_with_progress: job {crawl_id} started for {url}")

                consecutive_403s = 0  # consecutive pages with HTTP 403
                # How many consecutive "completed/0/0" responses we have seen.
                # Firecrawl sometimes returns this immediately on job creation before
                # workers have started; we keep polling until something changes.
                premature_done_count = 0
                MAX_PREMATURE_DONE = 10  # ~20 s at 2 s poll_interval

                base_url = (self._api_base_url or "https://api.firecrawl.dev").rstrip("/")
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                }

                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
                    while True:
                        if job_state.get("cancelled"):
                            log.info(f"crawl_with_progress: cancelling {crawl_id}")
                            try:
                                await async_app.cancel_crawl(crawl_id)
                            except Exception as ce:
                                log.warning(f"cancel call failed for {crawl_id}: {ce}")
                            return []

                        try:
                            resp = await http.get(f"{base_url}/v2/crawl/{crawl_id}", headers=headers)
                            body = resp.json()
                        except httpx.TimeoutException:
                            log.warning(f"crawl_with_progress {crawl_id}: status poll timed out (30 s) — treating as terminal")
                            job_state["cancelled"] = True
                            job_state["cancel_reason"] = "Firecrawl did not respond to status poll within 30 s"
                            try:
                                await async_app.cancel_crawl(crawl_id)
                            except Exception as ce:
                                log.warning(f"cancel call failed for {crawl_id}: {ce}")
                            break
                        except Exception as poll_err:
                            log.debug(f"crawl_with_progress {crawl_id}: poll error {poll_err}, retrying")
                            await asyncio.sleep(poll_interval)
                            continue

                        status = body.get("status", "unknown")
                        completed = body.get("completed", 0)
                        total = body.get("total", 0)
                        raw_data = body.get("data") or []

                        # Firecrawl v2 paginates the data array (~100 items per page).
                        # Only follow "next" links once we've consumed everything in
                        # the current first page, to avoid unnecessary requests while
                        # the crawl is still filling the first batch.
                        next_url = body.get("next")
                        log.info(f"crawl_with_progress {crawl_id}: next_url={next_url} processed={processed} raw_data_len={len(raw_data)}")
                        if next_url and processed >= len(raw_data) and len(raw_data) > 0:
                            while next_url:
                                try:
                                    log.info(f"crawl_with_progress {crawl_id}: fetching next page: {next_url}")
                                    next_resp = await http.get(next_url, headers=headers)
                                    if next_resp.status_code != 200:
                                        log.warning(
                                            f"crawl_with_progress {crawl_id}: pagination returned "
                                            f"HTTP {next_resp.status_code}, stopping"
                                        )
                                        break
                                    resp_text = next_resp.text
                                    if not resp_text or not resp_text.strip():
                                        log.warning(f"crawl_with_progress {crawl_id}: pagination returned empty body, stopping")
                                        break
                                    next_body = next_resp.json()
                                    next_page_data = next_body.get("data") or []
                                    if not next_page_data:
                                        break
                                    raw_data.extend(next_page_data)
                                    log.info(
                                        f"crawl_with_progress {crawl_id}: pagination added "
                                        f"{len(next_page_data)} items, raw_data_len now {len(raw_data)}"
                                    )
                                    next_url = next_body.get("next")
                                except Exception as page_err:
                                    log.warning(f"crawl_with_progress {crawl_id}: pagination fetch failed: {page_err}")
                                    break

                        job_state["completed"] = completed
                        job_state["total"] = total

                        log.info(
                            f"crawl_with_progress {crawl_id}: status={status} "
                            f"completed={completed}/{total} raw_data_len={len(raw_data)} processed={processed}"
                        )

                        # Append any newly scraped pages
                        cancelled_by_403 = False
                        for page in raw_data[processed:]:
                            markdown, source, title, status_code = self._extract_page(page, url)

                            if status_code == 403:
                                consecutive_403s += 1
                                log.warning(
                                    f"crawl_with_progress {crawl_id}: 403 on {source} "
                                    f"({consecutive_403s} consecutive)"
                                )
                                if max_consecutive_403s is not None and consecutive_403s >= max_consecutive_403s:
                                    log.warning(
                                        f"crawl_with_progress {crawl_id}: hit {consecutive_403s} consecutive "
                                        f"403s (limit={max_consecutive_403s}) — cancelling"
                                    )
                                    job_state["cancelled"] = True
                                    job_state["cancel_reason"] = (
                                        f"Cancelled after {consecutive_403s} consecutive 403 responses"
                                    )
                                    cancelled_by_403 = True
                                    try:
                                        await async_app.cancel_crawl(crawl_id)
                                    except Exception as ce:
                                        log.warning(f"cancel call failed for {crawl_id}: {ce}")
                                    processed += 1
                                    break
                            else:
                                consecutive_403s = 0

                            if markdown:
                                job_state["pages"].append({"url": source, "title": title, "content": markdown})
                                docs.append(LCDocument(
                                    page_content=markdown,
                                    metadata={"source": source, "title": title},
                                ))
                            processed += 1

                        # Track position in Firecrawl's data array for persistence/resume
                        job_state["_firecrawl_processed"] = processed
                        if on_progress:
                            result = on_progress(job_state)
                            if asyncio.iscoroutine(result):
                                await result

                        if cancelled_by_403:
                            break

                        if status in ("completed", "failed", "cancelled"):
                            if status == "completed" and completed == 0 and total == 0:
                                # Workers haven't started yet — keep polling up to MAX_PREMATURE_DONE
                                if premature_done_count < MAX_PREMATURE_DONE:
                                    premature_done_count += 1
                                    log.debug(
                                        f"crawl_with_progress {crawl_id}: premature completed 0/0 "
                                        f"(attempt {premature_done_count}/{MAX_PREMATURE_DONE}), waiting…"
                                    )
                                else:
                                    log.warning(f"crawl_with_progress {crawl_id}: timed out waiting for workers to start")
                                    break
                            elif status == "completed" and total > 0 and completed < total:
                                # Firecrawl prematurely reports "completed" mid-crawl — keep polling
                                log.debug(
                                    f"crawl_with_progress {crawl_id}: 'completed' but only "
                                    f"{completed}/{total} pages done, continuing…"
                                )
                            else:
                                if status == "completed":
                                    log.info(f"crawl_with_progress {crawl_id}: done, {len(docs)} pages")
                                else:
                                    log.error(f"crawl_with_progress {crawl_id}: {status}")
                                break

                        await asyncio.sleep(poll_interval)

            except Exception as e:
                log.error(f"crawl_with_progress error for {url}: {e}", exc_info=True)

        log.info(f"crawl_with_progress: done, {len(docs)} docs from {len(self.urls)} url(s)")
        return docs

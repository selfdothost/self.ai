"""web_crawl — a KB domain crawl driven by a model
(context/treasuremaps/2026-07-23-selfai-web-crawl-toggle.md).

The model chooses *what* to crawl; the destination knowledge base is bound
server-side from the user's selection and is never a tool argument. Write access
to that KB is re-checked at tool-run time, because the picker is a convenience
and the request metadata is untrusted input — the side effect here is writing
into someone's knowledge base.

Fire-and-report: the tool starts the same background crawl the KB UI runs and
returns a handle; it never waits for the crawl.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.utils.middleware import (
    WEB_CRAWL_TOOL_SPEC,
    run_web_crawl_tool_call,
)

KB_ID = "kb-123"
OWNER = "owner-1"
OTHER = "other-9"


@pytest.fixture(autouse=True)
def _no_dns():
    """validate_url resolves hostnames for its private-IP guard. These tests use
    non-resolving .example domains and are about the tool's gating, not about
    URL validation — so stub it out rather than depend on DNS."""
    with patch("selfai_ui.utils.middleware.validate_url", return_value=True):
        yield


def _request(enable=True, firecrawl=True, max_pages=25, max_depth=2):
    config = SimpleNamespace(
        ENABLE_WEB_CRAWL=enable,
        WEB_CRAWL_MAX_PAGES=max_pages,
        WEB_CRAWL_MAX_DEPTH=max_depth,
        FIRECRAWL_API_KEY="k" if firecrawl else "",
        FIRECRAWL_API_BASE_URL="http://fc" if firecrawl else "",
        USER_PERMISSIONS={"features": {"web_browsing": True}},
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _extra(kb_id=KB_ID, feature=True):
    md = {"features": {"web_crawl": feature}}
    if kb_id is not None:
        md["web_crawl_kb_id"] = kb_id
    return {"__event_emitter__": AsyncMock(), "__metadata__": md}


def _user(uid=OWNER, role="admin"):
    return SimpleNamespace(id=uid, role=role)


def _kb(owner=OWNER, access_control=None, name="Research"):
    return SimpleNamespace(id=KB_ID, user_id=owner, access_control=access_control, name=name)


def _run(request, url="https://site.example", extra=None, user=None):
    return asyncio.run(run_web_crawl_tool_call(request, url, extra or _extra(), user or _user()))


# --- the tool contract ----------------------------------------------------


@pytest.mark.tier0
def test_spec_takes_only_a_url_and_never_a_destination():
    """The model picks what to crawl, never where it lands."""
    props = WEB_CRAWL_TOOL_SPEC.input_schema["properties"]
    assert list(props) == ["url"]
    for forbidden in ("collection_name", "knowledge_id", "kb", "kb_id", "limit", "max_depth"):
        assert forbidden not in props
    assert WEB_CRAWL_TOOL_SPEC.name == "web_crawl"


# --- write access: the security-critical path -----------------------------


@pytest.mark.tier0
def test_refuses_when_user_lacks_write_access_and_never_crawls():
    request = _request()
    # Owned by someone else, and no access_control grant for this user.
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb(owner=OTHER)),
        patch("selfai_ui.utils.middleware.has_access", return_value=False),
        patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job,
        patch("selfai_ui.utils.middleware._run_crawl_background") as mock_run,
    ):
        out = _run(request, user=_user(uid=OWNER))

    assert "do not have write access" in out.lower()
    mock_job.assert_not_called()
    mock_run.assert_not_called()


@pytest.mark.tier0
def test_owner_may_crawl_into_their_own_kb():
    request = _request()
    started = {}

    def _mk_job(req, form, collection, uid):
        started["collection"] = collection
        started["url"] = form.url
        started["limit"] = form.limit
        started["max_depth"] = form.max_depth
        return {"job_id": "job-1"}

    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb(owner=OWNER)),
        patch("selfai_ui.utils.middleware.create_crawl_job", side_effect=_mk_job),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        out = _run(request, user=_user(uid=OWNER))

    assert "Started crawling" in out
    assert started["collection"] == KB_ID, "the crawl must target the bound KB's collection"


@pytest.mark.tier0
def test_a_granted_non_owner_may_crawl():
    request = _request()
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb(owner=OTHER)),
        patch("selfai_ui.utils.middleware.has_access", return_value=True),
        patch("selfai_ui.utils.middleware.create_crawl_job", return_value={"job_id": "job-2"}),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        out = _run(request, user=_user(uid=OWNER))

    assert "Started crawling" in out


@pytest.mark.tier0
def test_missing_kb_binding_is_refused():
    request = _request()
    with patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job:
        out = _run(request, extra=_extra(kb_id=None))
    assert "No knowledge base is selected" in out
    mock_job.assert_not_called()


@pytest.mark.tier0
def test_a_vanished_kb_is_refused():
    request = _request()
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=None),
        patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job,
    ):
        out = _run(request)
    assert "no longer exists" in out
    mock_job.assert_not_called()


# --- gating ---------------------------------------------------------------


@pytest.mark.tier0
def test_admin_flag_off_refuses_even_with_a_bound_kb():
    """A stale client must not keep writing into KBs after an admin disables it."""
    request = _request(enable=False)
    with patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job:
        out = _run(request)
    assert "not enabled on this instance" in out
    mock_job.assert_not_called()


@pytest.mark.tier0
def test_conversation_feature_off_refuses():
    request = _request()
    with patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job:
        out = _run(request, extra=_extra(feature=False))
    assert "not enabled for this conversation" in out
    mock_job.assert_not_called()


@pytest.mark.tier0
def test_browsing_permission_is_required():
    request = _request()
    request.app.state.config.USER_PERMISSIONS = {"features": {"web_browsing": False}}
    with patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job:
        out = _run(request, user=_user(role="user"))
    assert "not permitted" in out
    mock_job.assert_not_called()


@pytest.mark.tier0
def test_unconfigured_firecrawl_is_stated_not_crashed():
    request = _request(firecrawl=False)
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job") as mock_job,
    ):
        out = _run(request)
    assert "not configured" in out
    mock_job.assert_not_called()


# --- budget is server-side ------------------------------------------------


@pytest.mark.tier0
def test_budget_comes_from_admin_config_not_the_model():
    request = _request(max_pages=7, max_depth=1)
    seen = {}

    def _mk_job(req, form, collection, uid):
        seen["limit"] = form.limit
        seen["max_depth"] = form.max_depth
        return {"job_id": "job-3"}

    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job", side_effect=_mk_job),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        _run(request)

    assert seen == {"limit": 7, "max_depth": 1}


# --- fire-and-report ------------------------------------------------------


@pytest.mark.tier0
def test_returns_a_handle_without_awaiting_the_crawl():
    """The crawl must not be awaited — the tool returns while it still runs."""
    request = _request()
    finished = {"done": False}

    async def _slow_crawl(*a, **k):
        await asyncio.sleep(5)
        finished["done"] = True

    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job", return_value={"job_id": "job-4"}),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=_slow_crawl),
    ):
        out = _run(request)

    assert "Started crawling" in out
    assert "job-4" in out
    assert finished["done"] is False, "the tool must not wait for the crawl to finish"


@pytest.mark.tier0
def test_the_handle_tells_the_model_not_to_summarize_yet():
    """Fire-and-report: the model has no page content, so it must not pretend to."""
    request = _request()
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job", return_value={"job_id": "job-5"}),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        out = _run(request)

    assert "background" in out.lower()
    assert "not summarize" in out.lower()


@pytest.mark.tier0
def test_status_emits_its_own_action_and_terminates():
    request = _request()
    extra = _extra()
    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job", return_value={"job_id": "job-6"}),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        _run(request, extra=extra)

    statuses = [c.args[0]["data"] for c in extra["__event_emitter__"].call_args_list]
    assert statuses, "web_crawl emitted no status"
    assert all(s["action"] == "web_crawl" for s in statuses)
    assert statuses[0]["done"] is False
    assert statuses[-1]["done"] is True
    assert statuses[-1]["knowledge_name"] == "Research"


@pytest.mark.tier0
def test_a_bare_host_gets_https_and_a_bad_scheme_is_refused():
    request = _request()
    seen = {}

    def _mk_job(req, form, collection, uid):
        seen["url"] = form.url
        return {"job_id": "job-7"}

    with (
        patch("selfai_ui.utils.middleware.Knowledges.get_knowledge_by_id", return_value=_kb()),
        patch("selfai_ui.utils.middleware.create_crawl_job", side_effect=_mk_job),
        patch("selfai_ui.utils.middleware._run_crawl_background", new=AsyncMock()),
    ):
        _run(request, url="site.example/docs")

    assert seen["url"] == "https://site.example/docs"


# --- the metadata allowlist (regression) ----------------------------------


@pytest.mark.tier0
def test_web_crawl_kb_id_survives_the_metadata_allowlist():
    """main.py builds `metadata` from an explicit allowlist of top-level fields.
    The client sends web_crawl_kb_id at the top level of the body, so it is
    dropped unless it is named there — and then the tool is never offered
    because the bound KB looks absent.

    This shipped broken: every other test in this file hand-builds metadata, so
    none of them crossed the boundary where the drop happens. Asserted against
    the real source rather than a reconstruction, so it fails if the field is
    removed from the allowlist again.
    """
    from pathlib import Path

    main_src = Path(__file__).resolve().parents[1] / "selfai_ui" / "main.py"
    src = main_src.read_text()

    start = src.index('metadata = {\n            "user_id": user.id,')
    end = src.index('form_data["metadata"] = metadata', start)
    block = src[start:end]

    assert '"features"' in block, "features must be carried into metadata"
    assert '"web_crawl_kb_id"' in block, (
        "web_crawl_kb_id must be named in the metadata allowlist, or the "
        "Web Crawl tool is never offered even with the toggle on"
    )
    # Popped, not merely read: it must not remain in the payload sent to the model.
    assert 'form_data.pop("web_crawl_kb_id"' in block

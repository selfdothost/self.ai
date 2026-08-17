"""T-304 (LR/R3): re-score one position, and be honest about what came back.

The properties worth guarding here are the ones that fail QUIETLY:

  * an upstream that answered without a distribution must not arrive looking
    like a model that was certain (AC7) -- both render as an empty list;
  * the echoed sampling parameters must be the ones actually used, not the ones
    requested, because a model's configured params OVERWRITE the request
    (`apply_model_params_to_body` does `form_data[key] = value`) -- an echo
    that can disagree with the generation is worse than no echo (AC5);
  * reading the wrong field pair for the sampling mode returns [] with no
    error, which is the trap the whole surface exists to close (AC2).

`extract_position_distribution` is tested directly rather than through the
router for the shape questions, and through the router for the status codes,
because the status code IS the contract for AC7 and AC8.
"""

import pytest

from selfai_ui.utils.tokenization import (
    MAX_CHARS_PER_TOKEN,
    SAMPLING_FIELD_NAMES,
    TokenizationPrefixTooLong,
    TokenizationUpstreamError,
    assert_prefix_fits,
    build_identity,
    classify_upstream_error,
    extract_position_distribution,
    inspect_residency,
    resolve_rescore_sampling,
    served_model_of,
    weights_path_from_status,
)


@pytest.fixture(autouse=True)
def _no_identity_network(monkeypatch):
    """Keep the weights lookup off the network by default.

    Without this every endpoint test that reaches a 200 pays the real
    llamolotl model-list timeout -- measured at ~6.15s each, which is how the
    slow-test problem announced itself. Tests that care about identity patch
    this with their own value.
    """
    async def _none(request, model_id):
        return None

    monkeypatch.setattr("selfai_ui.routers.tokenization._served_status", _none, raising=False)


def _response(entry: dict) -> dict:
    return {"choices": [{"logprobs": {"content": [entry]}}]}


class TestReturnsCandidates:
    """R3-AC1 — top-N candidates with their probabilities."""

    def test_pre_sampling_reads_the_pre_sampling_fields(self):
        chosen, alts = extract_position_distribution(
            _response(
                {
                    "token": " the",
                    "id": 279,
                    "logprob": -0.12,
                    "top_logprobs": [
                        {"token": " the", "id": 279, "logprob": -0.12},
                        {"token": " a", "id": 264, "logprob": -2.30},
                    ],
                }
            ),
            post_sampling=False,
        )
        assert chosen == {"token": " the", "id": 279, "logprob": -0.12}
        assert [a["token"] for a in alts] == [" the", " a"]
        assert alts[1]["logprob"] == -2.30

    def test_post_sampling_reads_the_post_sampling_fields(self):
        """The field names CHANGE with the mode. Reading `logprob` here would
        return None for every value and an empty alternatives list, with
        nothing raised anywhere."""
        chosen, alts = extract_position_distribution(
            _response(
                {
                    "token": " the",
                    "id": 279,
                    "prob": 0.88,
                    "top_probs": [{"token": " the", "id": 279, "prob": 0.88}],
                }
            ),
            post_sampling=True,
        )
        assert chosen == {"token": " the", "id": 279, "prob": 0.88}
        assert alts == [{"token": " the", "id": 279, "prob": 0.88}]

    def test_reading_the_wrong_mode_is_visibly_empty_not_silently_wrong(self):
        """Guards the trap itself: a post-sampling response read as
        pre-sampling yields no values. This test exists so that a future change
        which 'simplifies' SAMPLING_FIELD_NAMES to a single pair fails here
        rather than in a view that renders nothing."""
        chosen, alts = extract_position_distribution(
            _response({"token": " x", "id": 1, "prob": 0.5, "top_probs": [{"token": " x"}]}),
            post_sampling=False,
        )
        assert chosen["logprob"] is None
        assert alts == []

    def test_a_certain_model_has_a_chosen_token_and_no_alternatives(self):
        """The legitimate empty case, which AC7's failures must not resemble."""
        chosen, alts = extract_position_distribution(
            _response({"token": " x", "id": 1, "logprob": 0.0, "top_logprobs": []}),
            post_sampling=False,
        )
        assert chosen["token"] == " x"
        assert alts == []


class TestUpstreamFailureIsDistinguishable:
    """R3-AC7 — 'we do not know' must not look like 'no alternatives'."""

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"choices": []},
            {"choices": [{}]},
            {"choices": [{"logprobs": None}]},
            {"choices": [{"logprobs": {"content": []}}]},
        ],
        ids=["empty", "no-choices", "no-logprobs-key", "null-logprobs", "empty-content"],
    )
    def test_a_response_without_a_distribution_raises(self, response):
        with pytest.raises(TokenizationUpstreamError):
            extract_position_distribution(response, post_sampling=False)

    def test_the_certain_case_does_not_raise(self):
        """The mutation guard for the tests above: if the failure check were
        widened to 'no alternatives', this legitimate response would start
        raising and the endpoint would report a fault where there is none."""
        chosen, alts = extract_position_distribution(
            _response({"token": " x", "id": 1, "logprob": 0.0, "top_logprobs": []}),
            post_sampling=False,
        )
        assert alts == []


class TestOverLongPrefix:
    """R3-AC8 — refused specifically, never truncated silently."""

    def test_a_prefix_that_certainly_exceeds_context_is_refused(self):
        model = {"id": "m", "owned_by": "llamolotl", "context_length": 10}
        with pytest.raises(TokenizationPrefixTooLong):
            assert_prefix_fits(model, "x" * (11 * MAX_CHARS_PER_TOKEN + 1))

    def test_an_ordinary_prefix_is_allowed(self):
        model = {"id": "m", "owned_by": "llamolotl", "context_length": 4096}
        assert_prefix_fits(model, "hello world") is None

    def test_the_bound_never_refuses_a_prefix_that_might_fit(self):
        """The guard bounds the token count from BELOW, so it may only refuse
        when refusal is CERTAIN.

        The length is chosen to separate a true lower bound from a plausible
        estimate: 800 characters is 50 tokens at chars/16 (fits a 100-token
        context, so this must pass) but 200 tokens at the common chars/4
        rule of thumb (which would refuse a prefix that fits perfectly well).
        Sized deliberately -- an earlier version of this test used a string
        short enough that both rules agreed, and so proved nothing."""
        model = {"id": "m", "owned_by": "llamolotl", "context_length": 100}
        assert_prefix_fits(model, "x" * 800) is None

    def test_an_unknown_context_defers_rather_than_refusing(self):
        assert_prefix_fits({"id": "m", "owned_by": "llamolotl"}, "x" * 10_000) is None
        assert_prefix_fits({"id": "m", "context_length": 0}, "x" * 10_000) is None

    def test_an_upstream_context_overflow_maps_to_the_same_specific_error(self):
        """Whether the prefix or the model notices should not change what the
        artist is told, because the remedy is identical."""
        err = classify_upstream_error("the request exceeds the available context size")
        assert isinstance(err, TokenizationPrefixTooLong)

    def test_an_unrelated_upstream_failure_stays_generic(self):
        err = classify_upstream_error("connection reset by peer")
        assert isinstance(err, TokenizationUpstreamError)
        assert not isinstance(err, TokenizationPrefixTooLong)


class TestSamplingEcho:
    """R3-AC5 — accepted and echoed, so a re-score reproduces what was seen."""

    def test_the_distribution_changing_parameters_are_kept(self):
        out = resolve_rescore_sampling(
            {"temperature": 0.8, "top_p": 0.9, "top_k": 40, "seed": 7}
        )
        assert out == {"temperature": 0.8, "top_p": 0.9, "top_k": 40, "seed": 7}

    def test_unrelated_parameters_are_dropped(self):
        """`max_tokens` in particular: a re-score reads one position, so
        accepting it would only let a caller pay for tokens nobody reads."""
        out = resolve_rescore_sampling({"temperature": 0.5, "max_tokens": 500, "stream": True})
        assert out == {"temperature": 0.5}

    def test_absent_parameters_are_not_invented(self):
        assert resolve_rescore_sampling(None) == {}
        assert resolve_rescore_sampling({}) == {}

    def test_an_explicit_zero_survives(self):
        """Filtered on `is not None`, not on truthiness -- temperature 0 is a
        meaningful request and the value most likely to be used deliberately."""
        assert resolve_rescore_sampling({"temperature": 0}) == {"temperature": 0}


class TestTheEndpoint:
    """The status code IS the contract for AC6, AC7 and AC8, so these go
    through the router rather than the helper."""

    MODEL = {"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096}

    @pytest.fixture
    def rescore(self, test_app, authenticated_user, monkeypatch):
        """Wire a llamolotl-backed model, the Studio permission, and a stub.

        The permission is granted explicitly rather than relied upon: it
        defaults to False (config.py), so without this every test below would
        pass for the wrong reason -- a 401 is not a re-score.
        """
        test_app.state.MODELS = {"gem8y": dict(self.MODEL)}
        test_app.state.config.USER_PERMISSIONS = {"studio": {"tokenization": True}}
        sent = {}

        def _stub(response=None, raises=None):
            async def _fake(request, payload, user):
                sent["payload"] = payload
                if raises is not None:
                    raise raises
                return response

            monkeypatch.setattr(
                "selfai_ui.routers.tokenization.generate_chat_completion", _fake
            )
            return sent

        authenticated_user.stub = _stub
        return authenticated_user

    def _ok_response(self):
        return _response(
            {
                "token": " the",
                "id": 279,
                "logprob": -0.1,
                "top_logprobs": [{"token": " the", "id": 279, "logprob": -0.1}],
            }
        )

    def test_it_returns_candidates_and_states_the_mode(self, rescore):
        rescore.stub(response=self._ok_response())
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "Once upon", "position": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["candidates"][0]["token"] == " the"
        assert body["chosen"]["id"] == 279
        assert body["position"] == 3
        # AC2: the mode is STATED, with the field names to read for it.
        assert body["post_sampling_probs"] is False
        assert body["fields"] == SAMPLING_FIELD_NAMES[False]

    def test_it_asks_for_one_token_and_continues_the_prefix(self, rescore):
        """A re-score reads a position; it does not generate a reply."""
        sent = rescore.stub(response=self._ok_response())
        rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "Once upon", "top_logprobs": 7},
        )
        payload = sent["payload"]
        assert payload["max_tokens"] == 1
        assert payload["logprobs"] is True
        assert payload["top_logprobs"] == 7
        assert payload["stream"] is False
        assert payload["continue_final_message"] is True
        assert payload["messages"][-1] == {"role": "assistant", "content": "Once upon"}

    def test_sampling_parameters_reach_the_model_and_are_echoed(self, rescore):
        """AC5 -- and the echo must be of what was USED."""
        sent = rescore.stub(response=self._ok_response())
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={
                "model": "gem8y",
                "prefix": "hi",
                "sampling": {"temperature": 0.8, "top_p": 0.9, "max_tokens": 500},
            },
        )
        assert sent["payload"]["temperature"] == 0.8
        assert sent["payload"]["top_p"] == 0.9
        # max_tokens is not a sampling parameter here: it must not be honoured
        # from `sampling`, or a caller could turn a re-score into a generation.
        assert sent["payload"]["max_tokens"] == 1
        assert r.json()["sampling"] == {"temperature": 0.8, "top_p": 0.9}

    def test_a_non_llamolotl_model_is_refused(self, rescore, test_app):
        test_app.state.MODELS = {"gpt": {"id": "gpt", "owned_by": "openai"}}
        r = rescore.post("/api/v1/tokenization/rescore", json={"model": "gpt", "prefix": "hi"})
        assert r.status_code == 400
        assert "openai" in r.json()["detail"]

    def test_an_unknown_model_is_a_404(self, rescore):
        r = rescore.post("/api/v1/tokenization/rescore", json={"model": "nope", "prefix": "hi"})
        assert r.status_code == 404

    def test_an_over_long_prefix_is_413_not_a_generic_400(self, rescore):
        """AC8 -- specific, so the client can say 're-score a shorter span'
        rather than 'bad request'."""
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "x" * (4096 * MAX_CHARS_PER_TOKEN + 100)},
        )
        assert r.status_code == 413
        assert "context" in r.json()["detail"]

    def test_an_upstream_without_a_distribution_is_502_not_an_empty_200(self, rescore):
        """AC7. A 200 with `candidates: []` here would be indistinguishable
        from a model that was certain -- which is the whole failure this
        criterion exists to prevent."""
        rescore.stub(response={"choices": [{}]})
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 502
        assert "logprobs" in r.json()["detail"]

    def test_a_certain_model_still_returns_200_with_no_candidates(self, rescore):
        """The other side of AC7: an genuinely empty distribution is a
        SUCCESS. Without this, widening the 502 above would look correct."""
        rescore.stub(
            response=_response({"token": " x", "id": 1, "logprob": 0.0, "top_logprobs": []})
        )
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 200
        assert r.json()["candidates"] == []
        assert r.json()["chosen"]["token"] == " x"

    def test_a_lease_refusal_keeps_its_status_and_reason(self, rescore):
        """The admission checkpoint's 503 carries a machine-readable body.
        Flattening it is self.ai#138, made in the task router; do not repeat
        it here."""
        from fastapi import HTTPException

        rescore.stub(
            raises=HTTPException(
                status_code=503,
                detail={"detail": "GPU dedicated", "gpu_locked_by": "publish"},
            )
        )
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 503
        assert r.json()["detail"]["gpu_locked_by"] == "publish"

    def test_it_writes_nothing_to_the_database(self, rescore, db_session):
        """AC6, by before/after snapshot of every table rather than by reading
        the code. A re-score is a READ; the neighbouring feature (branch from
        this token) is the one that writes, and it is deliberately not here."""
        from sqlalchemy import inspect, text

        def snapshot():
            names = inspect(db_session.bind).get_table_names()
            return {
                n: db_session.execute(text(f'SELECT COUNT(*) FROM "{n}"')).scalar()
                for n in names
            }

        before = snapshot()
        rescore.stub(response=self._ok_response())
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "Once upon", "position": 3},
        )
        assert r.status_code == 200
        assert snapshot() == before


class TestThePermissionGate:
    """T-305 (R3-AC3) -- the deferred X-2 of the Studio shell build site.

    Deferred there because gating a request path that did not exist would have
    been a permission with no referent. It exists now.
    """

    MODEL = {"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096}
    BODY = {"model": "gem8y", "prefix": "hi"}

    @pytest.fixture(autouse=True)
    def _wire(self, test_app, monkeypatch):
        test_app.state.MODELS = {"gem8y": dict(self.MODEL)}

        async def _fake(request, payload, user):
            return _response(
                {"token": " x", "id": 1, "logprob": 0.0, "top_logprobs": []}
            )

        monkeypatch.setattr("selfai_ui.routers.tokenization.generate_chat_completion", _fake)

    def _grant(self, test_app, permissions):
        test_app.state.config.USER_PERMISSIONS = permissions

    def test_a_user_without_the_permission_is_refused(self, test_app, authenticated_user):
        self._grant(test_app, {"studio": {"tokenization": False}})
        r = authenticated_user.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 401

    def test_a_user_with_the_permission_is_admitted(self, test_app, authenticated_user):
        self._grant(test_app, {"studio": {"tokenization": True}})
        r = authenticated_user.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 200

    def test_an_admin_is_admitted_without_the_permission(self, test_app, authenticated_admin):
        self._grant(test_app, {"studio": {"tokenization": False}})
        r = authenticated_admin.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 200

    def test_studio_training_alone_does_not_admit(self, test_app, authenticated_user):
        """The key must be exactly `studio.tokenization`. A neighbouring Studio
        permission granting access here would be invisible in review -- both
        are 'a Studio permission the user holds'."""
        self._grant(test_app, {"studio": {"training": True, "tokenization": False}})
        r = authenticated_user.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 401

    def test_an_absent_key_denies_rather_than_defaults_open(self, test_app, authenticated_user):
        """`has_permission` denies on any MISSING level of the dotted path, so
        a mis-declared key fails closed. Asserted because the failure is
        indistinguishable from an ungranted user at the call site."""
        self._grant(test_app, {"studio": {}})
        assert authenticated_user.post(
            "/api/v1/tokenization/rescore", json=self.BODY
        ).status_code == 401
        self._grant(test_app, {})
        assert authenticated_user.post(
            "/api/v1/tokenization/rescore", json=self.BODY
        ).status_code == 401

    def test_the_gate_runs_before_the_model_lookup(self, test_app, authenticated_user):
        """Ordering, not just presence. If the unknown-model 404 were raised
        first, an unauthorised caller could enumerate which models exist by
        reading 404 against 401."""
        self._grant(test_app, {"studio": {"tokenization": False}})
        r = authenticated_user.post(
            "/api/v1/tokenization/rescore", json={"model": "does-not-exist", "prefix": "hi"}
        )
        assert r.status_code == 401, "an unknown model must not be distinguishable pre-auth"

    def test_the_permission_key_is_declared_in_the_default_blob(self):
        """A key enforced here but absent from the config default is invisible
        to `get_permissions`, so the client could never render a way to reach
        this endpoint even for a group that HAS it -- the silent failure
        self.ai#133 still has for `evaluations`."""
        from selfai_ui.routers.tokenization import TOKENIZATION_PERMISSION

        section, _, key = TOKENIZATION_PERMISSION.partition(".")
        from selfai_ui.config import USER_PERMISSIONS

        assert key in USER_PERMISSIONS.value[section]


class TestResidencyIsReadFromTheListCoreAlreadyHas:
    """T-306 (R3-AC4), the helper half.

    The three-valued answer is the entire design. Two-valued residency — the
    obvious version — turns "we have not fetched the model list yet" into "your
    model is not loaded", which refuses correct requests on a signal that was
    never taken. `utils/vram_admission.py` documents that mistake at length
    ("Unknown never blocks") and this helper is deliberately built the same way.
    """

    def test_a_loaded_model_is_resident(self):
        report = inspect_residency(
            {"gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "loaded"}}, "gem8y"
        )
        assert report.resident is True
        assert report.status == "loaded"

    def test_an_unloaded_model_is_not_resident(self):
        report = inspect_residency(
            {"gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "unloaded"}}, "gem8y"
        )
        assert report.resident is False
        assert report.status == "unloaded"

    def test_a_loading_model_is_not_resident_either(self):
        """`loading` means a load is UNDERWAY, not that one is unnecessary. The
        request would still wait on it, so it must still be reported — the
        status string is what tells the two apart, not the verdict."""
        report = inspect_residency(
            {"gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "loading"}}, "gem8y"
        )
        assert report.resident is False
        assert report.status == "loading"

    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "gem8y", "owned_by": "llamolotl"},
            {"id": "gem8y", "owned_by": "llamolotl", "status": None},
            {"id": "gem8y", "owned_by": "llamolotl", "status": ""},
        ],
        ids=["absent", "null", "empty"],
    )
    def test_an_unpublished_status_is_unknown_and_not_a_refusal(self, entry):
        """`resident is None`, which the router must NOT treat as False.

        Asserted as an identity check rather than `assert not report.resident`
        precisely because that weaker form passes for both None and False, and
        the difference between them is a working endpoint and a broken one."""
        report = inspect_residency({"gem8y": entry}, "gem8y")
        assert report.resident is None

    def test_a_model_absent_from_the_list_is_unknown_rather_than_not_resident(self):
        """The router 404s an unknown model before it ever gets here, so this
        case only arises from a list that has not been fetched. Refusing on it
        would be inventing a fact."""
        assert inspect_residency({}, "gem8y").resident is None
        assert inspect_residency(None, "gem8y").resident is None

    def test_it_names_the_model_holding_the_card(self):
        """llamolotl serves ONE model at a time, so "not yours" is only half the
        answer — "and this one instead" is what makes the swap legible."""
        report = inspect_residency(
            {
                "gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "unloaded"},
                "qwen3": {"id": "qwen3", "owned_by": "llamolotl", "status": "loaded"},
            },
            "gem8y",
        )
        assert report.resident is False
        assert report.loaded_model == "qwen3"

    def test_a_non_llamolotl_model_is_never_named_as_the_holder(self):
        """An OpenAI-backed entry does not occupy the 4090 and cannot be the
        reason a load is needed. Without the `owned_by` filter this would name a
        cloud model as the thing holding local VRAM."""
        report = inspect_residency(
            {
                "gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "unloaded"},
                "gpt": {"id": "gpt", "owned_by": "openai", "status": "loaded"},
            },
            "gem8y",
        )
        assert report.loaded_model is None

    def test_the_requested_model_is_never_reported_as_its_own_holder(self):
        report = inspect_residency(
            {"gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "loaded"}}, "gem8y"
        )
        assert report.loaded_model is None

    def test_nothing_loaded_names_nobody(self):
        report = inspect_residency(
            {
                "gem8y": {"id": "gem8y", "owned_by": "llamolotl", "status": "unloaded"},
                "qwen3": {"id": "qwen3", "owned_by": "llamolotl", "status": "unloaded"},
            },
            "gem8y",
        )
        assert report.resident is False
        assert report.loaded_model is None


class TestTheStatusFieldSurvivesTheModelListPlumbing:
    """The guard above is inert if `status` never reaches `app.state.MODELS`.

    Residency is read off the shared model list rather than a round trip, and
    the field it reads is copied there by `utils/models.py` from llamolotl's
    `/v1/models`. Drop that one line and `inspect_residency` returns `None`
    forever — UNKNOWN, which by design proceeds. The endpoint would go on
    passing every test above while never once reporting a load, which is the
    precise shape of a guard that cannot fail.
    """

    def _entries(self, served):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from selfai_ui.utils import models as models_util

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    config=SimpleNamespace(
                        ENABLE_OPENAI_API=False,
                        ENABLE_OLLAMA_API=False,
                        ENABLE_LLAMOLOTL_API=True,
                        ENABLE_ANTHROPIC_API=False,
                    )
                )
            )
        )
        with patch.object(
            models_util.llamolotl, "get_all_models", AsyncMock(return_value=served)
        ), patch.object(models_util, "get_function_models", AsyncMock(return_value=[])):
            return asyncio.run(models_util.get_all_base_models(request))

    def test_the_llamolotl_status_reaches_the_shared_model_entry(self):
        entries = self._entries({"data": [{"id": "gem8y", "status": "loaded"}]})
        assert entries[0]["status"] == "loaded"
        # And the round trip through the real plumbing agrees with the helper.
        assert inspect_residency({e["id"]: e for e in entries}, "gem8y").resident is True

    def test_an_unloaded_model_arrives_unloaded_rather_than_unknown(self):
        entries = self._entries({"data": [{"id": "gem8y", "status": "unloaded"}]})
        assert inspect_residency({e["id"]: e for e in entries}, "gem8y").resident is False

    def test_the_entries_are_stamped_llamolotl_so_the_holder_filter_matches(self):
        """`inspect_residency` filters candidate holders on `owned_by`, and this
        is where that value is stamped."""
        entries = self._entries({"data": [{"id": "gem8y", "status": "loaded"}]})
        assert entries[0]["owned_by"] == "llamolotl"


class TestLoadRequiredIsReportedNotWaitedOn:
    """T-306 (R3-AC4), the endpoint half.

    The failure being designed out is not an error — it is a SPINNER. A re-score
    against an older session's model needs a swap, that swap is subject to GPU
    window enforcement and the VRAM broker, and so "waiting" is a legitimate
    long state. Which is exactly why it has to be said out loud: an artist
    cannot tell a 90-second lawful load from a wedged request, and neither can a
    client.

    So the contract asserted here is REPORTING, not waiting: a status the caller
    can branch on, a body naming what holds the card, and — the part that makes
    it a report rather than a timeout — no dispatch at all.
    """

    LOADED = {"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096, "status": "loaded"}
    COLD = {"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096, "status": "unloaded"}

    @pytest.fixture
    def rescore(self, test_app, authenticated_user, monkeypatch):
        """The T-304 fixture shape, plus a settable model list.

        The permission is granted explicitly for the same reason it is there:
        it defaults to False, so without it a 401 would masquerade as every
        assertion below.
        """
        test_app.state.config.USER_PERMISSIONS = {"studio": {"tokenization": True}}
        sent = {}

        def _stub(response=None, raises=None):
            async def _fake(request, payload, user):
                sent["payload"] = payload
                if raises is not None:
                    raise raises
                return response

            monkeypatch.setattr(
                "selfai_ui.routers.tokenization.generate_chat_completion", _fake
            )
            return sent

        def _models(**by_id):
            test_app.state.MODELS = by_id

        authenticated_user.stub = _stub
        authenticated_user.models = _models
        authenticated_user.models(gem8y=dict(self.COLD))
        return authenticated_user

    BODY = {"model": "gem8y", "prefix": "Once upon", "position": 3}

    def _ok_response(self):
        return _response(
            {"token": " the", "id": 279, "logprob": -0.1, "top_logprobs": []}
        )

    def test_a_non_resident_model_is_reported_rather_than_dispatched(self, rescore):
        """The whole criterion in one test: 409 with a machine-readable reason,
        and — the half that distinguishes a report from a timeout — the upstream
        was never called, so there is nothing to hang on."""
        sent = rescore.stub(response=self._ok_response())
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 409, r.text
        body = r.json()["detail"]
        assert body["load_required"] is True
        assert body["model"] == "gem8y"
        assert body["retry_with"] == {"allow_load": True}
        assert "payload" not in sent, "a report must not dispatch — that is the hang"

    def test_the_report_names_the_model_holding_the_card(self, rescore):
        """One model at a time means the remedy is a SWAP, and an artist cannot
        weigh a swap they are not told about."""
        rescore.models(
            gem8y=dict(self.COLD),
            qwen3={"id": "qwen3", "owned_by": "llamolotl", "status": "loaded"},
        )
        rescore.stub(response=self._ok_response())
        body = rescore.post("/api/v1/tokenization/rescore", json=self.BODY).json()["detail"]
        assert body["loaded_model"] == "qwen3"
        assert "qwen3" in body["detail"]

    def test_a_load_already_underway_is_distinguishable_from_one_not_started(self, rescore):
        """Both are 409 — the caller cannot proceed either way — but the remedy
        differs: one is "wait", the other is "ask". `model_status` is carried
        verbatim so the client is not left inferring which it got."""
        rescore.models(gem8y={**self.COLD, "status": "loading"})
        rescore.stub(response=self._ok_response())
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 409
        assert r.json()["detail"]["model_status"] == "loading"

        rescore.models(gem8y=dict(self.COLD))
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.json()["detail"]["model_status"] == "unloaded"

    def test_load_required_is_distinguishable_from_every_other_outcome(self, rescore):
        """AC4 says DISTINGUISHABLE, and the only way to assert that is to
        produce the neighbours and compare. A caller must be able to separate
        "needs a load" from success, from an upstream failure, and from a
        permission refusal — and from the lease refusal, whose 503 means "wait
        for a window to end" rather than "ask for a load"."""
        from fastapi import HTTPException

        rescore.stub(response=self._ok_response())
        load_required = rescore.post("/api/v1/tokenization/rescore", json=self.BODY).status_code

        rescore.models(gem8y=dict(self.LOADED))
        ok = rescore.post("/api/v1/tokenization/rescore", json=self.BODY).status_code

        rescore.stub(response={"choices": [{}]})
        upstream = rescore.post("/api/v1/tokenization/rescore", json=self.BODY).status_code

        rescore.stub(
            raises=HTTPException(
                status_code=503, detail={"detail": "GPU dedicated", "gpu_locked_by": "publish"}
            )
        )
        lease = rescore.post("/api/v1/tokenization/rescore", json=self.BODY).status_code

        codes = [load_required, ok, upstream, lease]
        assert codes == [409, 200, 502, 503]
        assert len(set(codes)) == 4

    def test_a_resident_model_dispatches_with_no_opt_in_needed(self, rescore):
        """The common case must cost nothing: already-loaded is answered from
        the cached list without a report and without a flag."""
        rescore.models(gem8y=dict(self.LOADED))
        sent = rescore.stub(response=self._ok_response())
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 200, r.text
        assert sent["payload"]["model"] == "gem8y"

    def test_an_unknown_status_dispatches_rather_than_refusing(self, rescore):
        """UNKNOWN NEVER BLOCKS. A model list that published no status — a
        connection that does not report one, or a list not yet fetched — must
        behave exactly as it did before this task, or T-306 becomes an outage
        for callers it was never about."""
        rescore.models(gem8y={"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096})
        sent = rescore.stub(response=self._ok_response())
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 200, r.text
        assert "payload" in sent

    def test_allow_load_dispatches_instead_of_reporting(self, rescore):
        """Having been told, a caller can say go. Without this the endpoint
        could never serve an older session's model at all — which is the exact
        scenario the task exists for."""
        sent = rescore.stub(response=self._ok_response())
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={**self.BODY, "allow_load": True}
        )
        assert r.status_code == 200, r.text
        assert "payload" in sent

    def test_allow_load_defaults_off_so_the_first_ask_is_always_told(self, rescore):
        """A default of True would restore the silent wait verbatim. Asserted on
        the model rather than by behaviour so a flipped default fails here and
        names itself, instead of surfacing as a spinner in the Studio."""
        from selfai_ui.routers.tokenization import RescoreForm

        assert RescoreForm(model="m").allow_load is False

    def test_a_lease_refusal_survives_the_opt_in_intact(self, rescore):
        """T-306 must not swallow T-304's contract. With the load allowed, an
        admission refusal is still the answer, with its status and its
        machine-readable body — a load being permitted by the caller does not
        make it permitted by the GPU window."""
        from fastapi import HTTPException

        rescore.stub(
            raises=HTTPException(
                status_code=503,
                detail={"detail": "GPU dedicated", "gpu_locked_by": "training"},
            )
        )
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={**self.BODY, "allow_load": True}
        )
        assert r.status_code == 503
        assert r.json()["detail"]["gpu_locked_by"] == "training"

    def test_the_permission_gate_still_runs_first(self, rescore, test_app):
        """Ordering. A 409 naming the resident model is an information leak to
        a caller who may not use this surface at all — it reveals what is on the
        card and that the model they asked for exists."""
        test_app.state.config.USER_PERMISSIONS = {"studio": {"tokenization": False}}
        rescore.stub(response=self._ok_response())
        r = rescore.post("/api/v1/tokenization/rescore", json=self.BODY)
        assert r.status_code == 401

    def test_the_cheaper_parameter_refusals_still_run_first(self, rescore):
        """A non-llamolotl model and an over-long prefix are both facts about
        the REQUEST, fixable without touching the GPU. Reporting a load for
        them would send the artist to swap a model that was never going to
        answer."""
        rescore.stub(response=self._ok_response())
        over_long = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "x" * (4096 * MAX_CHARS_PER_TOKEN + 100)},
        )
        assert over_long.status_code == 413

        rescore.models(gpt={"id": "gpt", "owned_by": "openai", "status": "unloaded"})
        assert (
            rescore.post(
                "/api/v1/tokenization/rescore", json={"model": "gpt", "prefix": "hi"}
            ).status_code
            == 400
        )

    def test_the_report_writes_nothing(self, rescore, db_session):
        """AC6 holds on the refusal path too. A report is a read that declined
        to read."""
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        def snapshot():
            names = sa_inspect(db_session.bind).get_table_names()
            return {
                n: db_session.execute(text(f'SELECT COUNT(*) FROM "{n}"')).scalar()
                for n in names
            }

        rescore.stub(response=self._ok_response())
        before = snapshot()
        assert rescore.post("/api/v1/tokenization/rescore", json=self.BODY).status_code == 409
        assert snapshot() == before


class TestFieldNamesAreDeclared:
    """R3-AC2 — the response must STATE the mode, not leave it inferable."""

    def test_the_two_modes_use_different_field_names(self):
        assert SAMPLING_FIELD_NAMES[False] != SAMPLING_FIELD_NAMES[True]

    def test_each_mode_declares_both_names(self):
        for mode in (False, True):
            assert set(SAMPLING_FIELD_NAMES[mode]) == {"token", "alternatives"}


class TestServedModelDetection:
    """T-307 / R5-AC3 — which model ACTUALLY answered."""

    def test_a_substitution_record_wins_over_a_stale_echoed_model(self):
        """self.ai#35: an eval window serves the resident model instead.

        The echoed `model` and the substitution record are made to DISAGREE
        here on purpose. An earlier version of this test set both to the
        substitute, so every read order produced the right answer and the test
        could not fail — a mutation that ignored the substitution record
        entirely still passed it. The dangerous direction is precisely this
        one: the body echoes the model that was ASKED for while the record
        says another one answered, so reading `model` silently misses the swap.
        """
        assert (
            served_model_of(
                {"model": "asked", "model_substitution": {"requested": "asked", "served": "sub"}},
                "asked",
            )
            == "sub"
        )

    def test_the_record_also_wins_over_a_disagreeing_arena_channel(self):
        assert (
            served_model_of(
                {
                    "model": "asked",
                    "selected_model_id": "stale",
                    "model_substitution": {"requested": "asked", "served": "sub"},
                },
                "asked",
            )
            == "sub"
        )

    def test_the_arena_channel_is_read_when_there_is_no_record(self):
        assert served_model_of({"selected_model_id": "other"}, "asked") == "other"

    def test_an_echoed_model_is_used_next(self):
        assert served_model_of({"model": "echoed"}, "asked") == "echoed"

    def test_an_unhelpful_response_falls_back_to_the_request(self):
        assert served_model_of({}, "asked") == "asked"
        assert served_model_of(None, "asked") == "asked"
        assert served_model_of("not a dict", "asked") == "asked"


class TestWeightsIdentity:
    """T-307 / R5-AC2 — the GGUF, not the display name."""

    def test_the_argv_model_flag_is_preferred(self):
        status_obj = {
            "args": ["/app/llama-server", "--alias", "X", "--model", "/models/X/X-Q4.gguf"],
            "preset": "[X]\nmodel = /models/stale/other.gguf\n",
        }
        assert weights_path_from_status(status_obj) == "/models/X/X-Q4.gguf"

    def test_the_preset_is_the_fallback_when_nothing_is_running(self):
        """An unloaded model has no argv yet, but its preset still names the
        file — which is what makes identity available before a load."""
        assert (
            weights_path_from_status({"preset": "[X]\nctx-size = 4096\nmodel = /models/X.gguf\n"})
            == "/models/X.gguf"
        )

    def test_a_trailing_model_flag_with_no_value_does_not_crash(self):
        assert weights_path_from_status({"args": ["--model"]}) is None

    def test_absent_identity_is_reported_as_unknown_not_invented(self):
        ident = build_identity("m", None)
        assert ident["weights"] is None
        assert ident["weights_known"] is False

    def test_a_known_identity_says_so(self):
        ident = build_identity("m", {"args": ["--model", "/models/m.gguf"]}, context_length=4096)
        assert ident == {
            "model": "m",
            "weights": "/models/m.gguf",
            "weights_known": True,
            "context_length": 4096,
        }

    def test_a_preset_without_a_model_line_is_unknown_not_empty(self):
        """Stated as a flag so a consumer can tell "no weights line" from a
        weights value it should try to use — a bake that skips its parity check
        because it could not tell the difference is the failure R5 exists to
        prevent.

        NB this deliberately does NOT claim to distinguish `is not None` from
        truthiness: the parser normalises a blank value to None, so no input
        reaches `build_identity` with an empty-string path. An earlier version
        of this test asserted that distinction and could not fail."""
        ident = build_identity("m", {"preset": "[X]\nctx-size = 4096\n"})
        assert ident["weights_known"] is False
        assert ident["weights"] is None

    def test_a_blank_model_value_is_normalised_to_unknown(self):
        """The one place a blank could enter, checked at its source."""
        assert weights_path_from_status({"preset": "[X]\nmodel =   \n"}) is None


class TestIdentityOnTheEndpoint:
    """T-307 end to end."""

    MODEL = {"id": "gem8y", "owned_by": "llamolotl", "context_length": 4096}

    @pytest.fixture
    def rescore(self, test_app, authenticated_user, monkeypatch):
        test_app.state.MODELS = {"gem8y": dict(self.MODEL), "other": dict(self.MODEL, id="other")}
        test_app.state.config.USER_PERMISSIONS = {"studio": {"tokenization": True}}
        sent = {}

        async def _status(request, model_id):
            return {"args": ["--model", f"/models/{model_id}.gguf"]}

        monkeypatch.setattr("selfai_ui.routers.tokenization._served_status", _status)

        def _stub(response):
            async def _fake(request, payload, user):
                sent["payload"] = payload
                return response

            monkeypatch.setattr(
                "selfai_ui.routers.tokenization.generate_chat_completion", _fake
            )
            return sent

        authenticated_user.stub = _stub
        return authenticated_user

    def _ok(self, **extra):
        return {
            "choices": [
                {
                    "logprobs": {
                        "content": [
                            {
                                "token": " the",
                                "id": 279,
                                "logprob": -0.1,
                                "top_logprobs": [{"token": " the", "id": 279, "logprob": -0.1}],
                            }
                        ]
                    }
                }
            ],
            **extra,
        }

    def test_every_entry_carries_a_numeric_id(self, rescore):
        """R5-AC1. Rendered text alone is not enough: two different tokens can
        render identically, and a bake replays ids."""
        rescore.stub(self._ok())
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["chosen"]["id"] == 279
        assert all(isinstance(c["id"], int) for c in body["candidates"])

    def test_the_response_names_the_weights(self, rescore):
        """R5-AC2 — the display id is a preset section name and can be renamed;
        the GGUF is what a bake can compare against."""
        rescore.stub(self._ok())
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        ident = r.json()["identity"]
        assert ident["model"] == "gem8y"
        assert ident["weights"] == "/models/gem8y.gguf"
        assert ident["weights_known"] is True
        assert ident["context_length"] == 4096

    def test_a_substitution_is_refused_not_returned(self, rescore):
        """R5-AC3, the sharp case. The distribution is well-formed and entirely
        wrong: it belongs to another vocabulary. Returning it marked would still
        put untrustworthy numbers in front of an artist."""
        rescore.stub(
            self._ok(
                model="other",
                selected_model_id="other",
                model_substitution={"requested": "gem8y", "served": "other", "reason": "eval_window"},
            )
        )
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["model_mismatch"] is True
        assert d["served"] == "other" and d["requested"] == "gem8y"
        assert d["substitution"]["reason"] == "eval_window"

    def test_load_required_and_model_mismatch_are_distinguishable(self, rescore):
        """Both are 409. A client switches on the KEY, so the two must never
        both be present and must never be confused."""
        rescore.stub(
            self._ok(model="other", model_substitution={"requested": "gem8y", "served": "other"})
        )
        d = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        ).json()["detail"]
        assert d.get("model_mismatch") is True
        assert "load_required" not in d

    def test_an_expected_model_disagreement_is_refused_before_dispatch(self, rescore):
        """R5-AC3. Cheaper and more honest than answering: the caller has told
        us these ids came from elsewhere."""
        sent = rescore.stub(self._ok())
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "hi", "expected_model": "other"},
        )
        assert r.status_code == 400
        assert "other" in r.json()["detail"] and "gem8y" in r.json()["detail"]
        assert "payload" not in sent, "must refuse without generating"

    def test_a_matching_expected_model_is_admitted(self, rescore):
        rescore.stub(self._ok())
        r = rescore.post(
            "/api/v1/tokenization/rescore",
            json={"model": "gem8y", "prefix": "hi", "expected_model": "gem8y"},
        )
        assert r.status_code == 200

    def test_an_absent_expected_model_does_not_refuse(self, rescore):
        """Optional by design — an artist may re-score a prefix with no stored
        reply behind it. A required field here would break that."""
        rescore.stub(self._ok())
        assert (
            rescore.post(
                "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
            ).status_code
            == 200
        )

    def test_unreadable_weights_do_not_fail_the_rescore(self, rescore, monkeypatch):
        """The distribution is still correct and useful. But the response must
        SAY the identity is unknown rather than omit the question."""
        async def _none(request, model_id):
            return None

        monkeypatch.setattr("selfai_ui.routers.tokenization._served_status", _none)
        rescore.stub(self._ok())
        r = rescore.post(
            "/api/v1/tokenization/rescore", json={"model": "gem8y", "prefix": "hi"}
        )
        assert r.status_code == 200
        assert r.json()["identity"]["weights_known"] is False


class TestTheRelayPreservesIds:
    """R5-AC1 on the STREAMING half (utils/middleware.py, T-303).

    A source guard rather than a behaviour test: the relay hands the upstream
    `logprobs` object through untouched, and the way ids would be lost is
    someone 'tidying' it into a rebuilt dict of selected fields.
    """

    def test_the_relay_passes_the_logprobs_object_through_whole(self):
        import inspect

        from selfai_ui.utils import middleware

        src = inspect.getsource(middleware)
        assert 'data.get("choices", [])[0].get("logprobs")' in src, (
            "the relay must read the whole logprobs object; rebuilding it from "
            "named fields is how numeric token ids get dropped"
        )
        assert '"logprobs": logprobs_payload' in src, (
            "the payload must be relayed as-is, not reshaped"
        )

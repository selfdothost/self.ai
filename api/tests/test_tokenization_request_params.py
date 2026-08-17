"""T-301 (LR/R1): bound the tokenization logprobs parameters, and echo them.

`logprobs`/`top_logprobs` already reach llamolotl -- the chat entry takes a raw
dict with no field stripping -- so this is not a plumbing test. It guards two
properties:

  * an absurd `top_logprobs` FAILS rather than being clamped silently upstream
    (llama.cpp does `std::min(max_probs, n_probs_request)`, which succeeds and
    quietly returns fewer candidates than were asked for);
  * an ordinary chat request is untouched, which is what makes this safe to put
    on the path every message flows through.

The pre-sampling default is deliberate and measured; see the module docstring
of `utils/tokenization.py`.
"""

import pytest

from selfai_ui.utils.tokenization import (
    DEFAULT_POST_SAMPLING,
    MAX_TOP_LOGPROBS,
    SAMPLING_FIELD_NAMES,
    TokenizationParamError,
    resolve_logprobs_request,
)


class TestOrdinaryChatIsUntouched:
    """R1-AC4. The most important property here: this runs on every request."""

    def test_a_request_without_logprobs_resolves_to_none(self):
        assert resolve_logprobs_request({"model": "m", "messages": []}) is None

    def test_logprobs_false_is_not_a_tokenization_request(self):
        assert resolve_logprobs_request({"logprobs": False, "top_logprobs": 5}) is None

    def test_the_form_data_is_not_mutated(self):
        """The resolver READS. Anything that edited the payload here would be
        changing the request every chat message sends."""
        form = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        before = dict(form)
        resolve_logprobs_request(form)
        assert form == before


class TestBound:
    """R1-AC6 — fail fast, rather than truncate silently upstream."""

    def test_the_maximum_is_accepted(self):
        out = resolve_logprobs_request({"logprobs": True, "top_logprobs": MAX_TOP_LOGPROBS})
        assert out["top_logprobs"] == MAX_TOP_LOGPROBS

    def test_one_over_the_maximum_is_refused(self):
        with pytest.raises(TokenizationParamError) as e:
            resolve_logprobs_request({"logprobs": True, "top_logprobs": MAX_TOP_LOGPROBS + 1})
        # The message must name the bound and point at the alternative, since an
        # artist sees this text.
        assert str(MAX_TOP_LOGPROBS) in str(e.value)
        assert "re-scor" in str(e.value).lower()

    def test_absurd_values_are_refused_rather_than_clamped(self):
        with pytest.raises(TokenizationParamError):
            resolve_logprobs_request({"logprobs": True, "top_logprobs": 10_000})

    def test_negative_is_refused(self):
        with pytest.raises(TokenizationParamError):
            resolve_logprobs_request({"logprobs": True, "top_logprobs": -1})

    def test_zero_is_valid_and_is_the_streaming_default(self):
        """Zero is not "off" -- it still asks for the CHOSEN token's own
        probability, which is what the confidence heatmap renders. Alternatives
        are 92% of payload and are fetched per position on demand."""
        out = resolve_logprobs_request({"logprobs": True, "top_logprobs": 0})
        assert out["top_logprobs"] == 0
        assert out["logprobs"] is True

    def test_a_bool_is_not_an_integer_here(self):
        """bool is an int in Python, so `True` would otherwise sail through as a
        perfectly plausible top_logprobs=1."""
        with pytest.raises(TokenizationParamError):
            resolve_logprobs_request({"logprobs": True, "top_logprobs": True})

    def test_a_string_is_refused(self):
        with pytest.raises(TokenizationParamError):
            resolve_logprobs_request({"logprobs": True, "top_logprobs": "5"})


class TestSamplingModeEcho:
    """R1-AC2/AC3 and the added labelling criterion."""

    def test_the_default_is_pre_sampling(self):
        """REVERSED from this kit's original recommendation by the Phase 1
        spike: post-sampling returned 2 of 5 requested candidates because top_p
        had already truncated the set, and said nothing about it."""
        assert DEFAULT_POST_SAMPLING is False
        out = resolve_logprobs_request({"logprobs": True, "top_logprobs": 5})
        assert out["post_sampling_probs"] is False

    def test_post_sampling_can_be_selected(self):
        out = resolve_logprobs_request(
            {"logprobs": True, "top_logprobs": 5, "post_sampling_probs": True}
        )
        assert out["post_sampling_probs"] is True

    def test_the_echo_names_the_fields_for_the_mode(self):
        """THE TRAP THIS EXISTS FOR. llama.cpp renames the fields with the mode:
        pre emits logprob/top_logprobs, post emits prob/top_probs. A client
        reading the wrong pair sees an empty list that is indistinguishable from
        "the model was certain" -- no error anywhere."""
        pre = resolve_logprobs_request({"logprobs": True, "top_logprobs": 5})
        post = resolve_logprobs_request(
            {"logprobs": True, "top_logprobs": 5, "post_sampling_probs": True}
        )
        assert pre["fields"] == {"token": "logprob", "alternatives": "top_logprobs"}
        assert post["fields"] == {"token": "prob", "alternatives": "top_probs"}
        assert pre["fields"] != post["fields"]

    def test_the_field_map_covers_both_modes(self):
        assert set(SAMPLING_FIELD_NAMES) == {True, False}

    def test_a_non_boolean_mode_is_refused(self):
        with pytest.raises(TokenizationParamError):
            resolve_logprobs_request(
                {"logprobs": True, "top_logprobs": 5, "post_sampling_probs": "yes"}
            )


class TestAnthropicPathUnchanged:
    """R1-AC7. Decision 9 makes this surface local-only; the Anthropic path
    must keep dropping both params, and nothing here may loosen that."""

    def test_both_params_are_still_dropped_for_anthropic(self):
        from selfai_ui.utils.payload import ANTHROPIC_UNSUPPORTED_PARAMS

        assert "logprobs" in ANTHROPIC_UNSUPPORTED_PARAMS
        assert "top_logprobs" in ANTHROPIC_UNSUPPORTED_PARAMS


# ── T-302: refuse, do not downgrade ────────────────────────────────────────


class TestModelMustBeAbleToTokenize:
    def test_a_llamolotl_model_is_accepted(self):
        from selfai_ui.utils.tokenization import assert_model_can_tokenize

        assert_model_can_tokenize({"id": "gemma", "owned_by": "llamolotl"}) is None

    @pytest.mark.parametrize("owned_by", ["openai", "ollama", "anthropic", "arena", None])
    def test_every_other_provider_is_refused(self, owned_by):
        """REFUSE, not downgrade. A session that silently answers without
        distributions looks like a broken feature rather than an unsupported
        model, and the artist has no way to tell which."""
        from selfai_ui.utils.tokenization import assert_model_can_tokenize

        with pytest.raises(TokenizationParamError) as e:
            assert_model_can_tokenize({"id": "m", "owned_by": owned_by})
        # The message must name the model and the requirement, since it is shown.
        assert "'m'" in str(e.value)
        assert "llamolotl" in str(e.value)

    def test_a_missing_model_is_refused_rather_than_crashing(self):
        from selfai_ui.utils.tokenization import assert_model_can_tokenize

        with pytest.raises(TokenizationParamError):
            assert_model_can_tokenize({})


# ── T-303: the relay keeps distributions, and persists none of them ────────
#
# These assert on the RELAY'S SHAPE rather than by running the generator, which
# needs a live upstream. The properties that matter are structural and each one
# corresponds to a way the current code loses the data:
#
#   * ENABLE_REALTIME_CHAT_SAVE OFF replaces the chunk with `update` before
#     emitting -- the position where logprobs were being dropped
#   * `update` is what gets persisted, so anything added to it lands in a chat
#     blob that is loaded whole (order 1 MB per 1000-token reply)


class TestRelayPreservesDistributions:
    @staticmethod
    def _relay_source() -> str:
        from pathlib import Path

        import selfai_ui.utils.middleware as mw

        return Path(mw.__file__).read_text(encoding="utf-8")

    def test_the_relay_captures_logprobs_only_for_a_tokenization_session(self):
        src = self._relay_source()
        assert 'metadata.get("logprobs_settings")' in src, (
            "capture must be gated on the session, or every ordinary chat pays for it"
        )

    def test_the_off_path_reattaches_them_to_the_emitted_payload(self):
        """The ON path emits the raw chunk, so distributions ride along already.
        The OFF path replaces it with `update` -- this is the fix."""
        src = self._relay_source()
        assert '{**update, "logprobs": logprobs_payload}' in src

    def test_logprobs_are_never_added_to_the_persisted_update(self):
        """R2-AC4. `update` is the dict handed to upsert_message_to_chat_by_id.
        Adding logprobs to it would write ~1 MB of JSON per 1000-token reply
        into a column loaded whole."""
        src = self._relay_source()
        assert 'update["logprobs"]' not in src
        assert '"logprobs": logprobs_payload,' not in src.replace(
            '{**update, "logprobs": logprobs_payload}', ""
        )

    def test_a_malformed_payload_cannot_break_the_message(self):
        """R2-AC6. The capture is wrapped, so a chunk with an unexpected shape
        costs that chunk's distributions and nothing else."""
        src = self._relay_source()
        window = src[src.index("logprobs_payload = None") : src.index("logprobs_payload = None") + 500]
        assert "try:" in window and "except Exception:" in window

    def test_content_and_reasoning_accumulation_is_untouched(self):
        """R2-AC3. The two lines every ordinary chat depends on."""
        src = self._relay_source()
        assert 'value = delta.get("content")' in src
        assert 'reasoning_value = delta.get("reasoning_content")' in src
        assert 'content = f"{content}{value}"' in src

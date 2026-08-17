"""Render an `hf_task` definition into a language-eval task config (self.ai#91).

This lives in api-core deliberately. The harnesses are given finished YAML and
never learn about HuggingFace refs, so the templating exists once: reachable
from the API directly, not stranded in the client, and not duplicated across
self.language-eval and self.code-eval.

The shapes are taken from real vendored task configs rather than invented —
`tasks/sciq/sciq.yaml` for multiple_choice and `tasks/gsm8k/gsm8k.yaml` for
generate_until.

On the `{{...}}` fields
----------------------
`doc_to_text` / `doc_to_target` / `doc_to_choice` are lm-eval's own Jinja
templates and are passed through verbatim. lm-eval renders them with
`Environment(loader=BaseLoader, undefined=StrictUndefined)` — a plain
Environment, not a SandboxedEnvironment — so a hostile template is in principle
an SSTI vector inside the harness process.

That is worth naming, and it is *not* a new privilege: registering an eval is
admin-only, and the `raw_yaml` kind already lets an admin supply exactly these
fields. Templating here neither widens nor narrows that boundary. Narrowing it
would mean sandboxing lm-eval's renderer, which is a change to the harness, not
to this module — filed thinking, not silently assumed away.
"""

from typing import Any, Optional

import yaml

#: The two output types this templatiser supports. lm-eval has more
#: (loglikelihood, loglikelihood_rolling); they are omitted until something asks
#: for them rather than guessed at.
OUTPUT_MULTIPLE_CHOICE = "multiple_choice"
OUTPUT_GENERATE_UNTIL = "generate_until"
OUTPUT_TYPES = {OUTPUT_MULTIPLE_CHOICE, OUTPUT_GENERATE_UNTIL}

#: Defaults matched to the vendored configs for each output type.
_DEFAULT_METRICS = {
    OUTPUT_MULTIPLE_CHOICE: [
        {"metric": "acc", "aggregation": "mean", "higher_is_better": True},
        {"metric": "acc_norm", "aggregation": "mean", "higher_is_better": True},
    ],
    OUTPUT_GENERATE_UNTIL: [
        {
            "metric": "exact_match",
            "aggregation": "mean",
            "higher_is_better": True,
            "ignore_case": True,
            "ignore_punctuation": False,
        },
    ],
}

_SPLIT_KEYS = ("training_split", "validation_split", "test_split", "fewshot_split")


class TemplateError(ValueError):
    """The definition cannot produce a valid task config."""


def validate_hf_task_definition(definition: dict) -> None:
    """Raise :class:`TemplateError` if the definition cannot be rendered.

    Deliberately strict about the fields that decide what is *measured*. A task
    that renders but scores the wrong column is worse than one that refuses to
    render, because its number looks real.
    """
    if not isinstance(definition, dict):
        raise TemplateError("definition must be an object")

    dataset_path = definition.get("dataset_path")
    if not dataset_path or not isinstance(dataset_path, str):
        raise TemplateError("dataset_path is required (the HuggingFace dataset id, e.g. 'allenai/sciq')")

    output_type = definition.get("output_type")
    if output_type not in OUTPUT_TYPES:
        raise TemplateError(f"output_type must be one of: {', '.join(sorted(OUTPUT_TYPES))}")

    if not definition.get("doc_to_text"):
        raise TemplateError("doc_to_text is required — it is the prompt the model sees")

    if definition.get("doc_to_target") in (None, ""):
        raise TemplateError("doc_to_target is required — it is what the answer is scored against")

    if output_type == OUTPUT_MULTIPLE_CHOICE and not definition.get("doc_to_choice"):
        raise TemplateError("doc_to_choice is required for multiple_choice — it is the set of options")

    # A task with no evaluation split has nothing to score. lm-eval would fail
    # at run time; failing here means the admin finds out at registration.
    if not any(definition.get(k) for k in ("test_split", "validation_split")):
        raise TemplateError("at least one of test_split or validation_split is required")

    metrics = definition.get("metric_list")
    if metrics is not None:
        if not isinstance(metrics, list) or not metrics:
            raise TemplateError("metric_list, if given, must be a non-empty list")
        for m in metrics:
            if not isinstance(m, dict) or not m.get("metric"):
                raise TemplateError("each metric_list entry must be an object with a 'metric' key")


def render_hf_task_yaml(name: str, definition: dict, description: Optional[str] = None) -> str:
    """Render an `hf_task` definition to a language-eval task config.

    The output is ordinary data YAML — no custom tags — which is what lets the
    harness accept it under `yaml.safe_load` (self.language-eval#2).
    """
    validate_hf_task_definition(definition)

    config: dict[str, Any] = {
        "task": name,
        "dataset_path": definition["dataset_path"],
        "output_type": definition["output_type"],
    }

    # `dataset_name` is the HF *config* name. Emitted explicitly as null when
    # absent, matching the vendored configs (sciq.yaml carries `dataset_name: null`).
    config["dataset_name"] = definition.get("dataset_name") or None

    for key in _SPLIT_KEYS:
        if definition.get(key):
            config[key] = definition[key]

    config["doc_to_text"] = definition["doc_to_text"]
    config["doc_to_target"] = definition["doc_to_target"]
    if definition["output_type"] == OUTPUT_MULTIPLE_CHOICE:
        config["doc_to_choice"] = definition["doc_to_choice"]

    if definition.get("num_fewshot") is not None:
        config["num_fewshot"] = definition["num_fewshot"]
    if definition.get("generation_kwargs"):
        config["generation_kwargs"] = definition["generation_kwargs"]

    config["metric_list"] = definition.get("metric_list") or _DEFAULT_METRICS[definition["output_type"]]

    metadata: dict[str, Any] = {"version": 1.0}
    if description:
        metadata["description"] = description
    # Provenance: a config found on the harness PVC should say where it came
    # from, or the next operator has a mystery file with no owner.
    metadata["source"] = "self.ai custom evaluation"
    config["metadata"] = metadata

    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False)


def render_definition(kind: str, name: str, definition: dict, description: Optional[str] = None) -> str:
    """Render whatever the catalog holds into the YAML the harness is given.

    `raw_yaml` passes through untouched — the admin authored it and the harness
    validates it on receipt, so re-serialising here would only risk changing
    something they meant.
    """
    if kind == "raw_yaml":
        raw = definition.get("yaml")
        if not raw or not isinstance(raw, str):
            raise TemplateError("raw_yaml definitions must carry a non-empty 'yaml' string")
        return raw
    if kind == "hf_task":
        return render_hf_task_yaml(name, definition, description)
    raise TemplateError(f"kind {kind!r} does not render to a task config")

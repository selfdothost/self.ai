"""Provider-neutral tool specification.

Cavekit: cavekit-toolspec-model.md R1 -- T-001, R2 -- T-003/T-004, R3 -- T-005,
R4 criterion 1 -- T-007.

This module is deliberately import-light: pydantic and stdlib only. It carries no
dependency on ``models.tools``, ``utils.plugin``, the DB layer, or FastAPI, so
``selfai_ui/modapi/__init__.py`` can re-export ``ToolSpec`` cheaply without
dragging the application into a mod's import graph.

Field names follow MCP -- the superset of the OpenAI, Anthropic, and MCP tool
wire formats -- in snake_case per Python convention. The camelCase MCP wire names
are a serializer concern, produced at the edge, and are not used as field names
here.
"""

import copy

from pydantic import BaseModel, ConfigDict, Field


class ToolSpecError(ValueError):
    """A stored tool spec cannot be read into the model.

    Raised only by ``from_openai``, and only for a spec whose keys core does not
    support. A ``ValueError`` subclass so an existing broad handler still catches
    it; a distinct type so a caller that wants to name the failing toolkit can.
    """


#: The keys a stored spec may carry, after ``parameters`` has been renamed to
#: ``input_schema``. Deliberately not derived from the model's fields: ``title``
#: and ``output_schema`` are real model fields, but nothing writes them to the
#: database, so a stored row carrying one is a row we did not write.
_STORED_KEYS = frozenset({"name", "description", "input_schema"})


def _empty_object_schema() -> dict:
    """Return a fresh empty-object JSON Schema.

    Used as a ``default_factory`` so that every default-constructed ``ToolSpec``
    owns its own ``input_schema`` dict. A shared mutable default would be handed
    out to callers on the live path, where an in-place edit to one spec's schema
    would silently corrupt every other.
    """
    return {"type": "object", "properties": {}}


def _is_internal(key: object) -> bool:
    """Return True for an injected-argument name the model must never be shown.

    ``__user__``, ``__id__``, ``__event_emitter__`` and friends are supplied by
    core, not by the LLM. The guard on ``str`` keeps a malformed schema body
    (a non-string entry in ``required``) from raising here rather than at the
    provider.
    """
    return isinstance(key, str) and key.startswith("__")


class ToolSpec(BaseModel):
    """The canonical, provider-neutral specification of one tool.

    The argument and result schemas are carried as plain ``dict``s. The model
    deliberately does not attempt to type the JSON-Schema body (``$ref``,
    ``oneOf``, nesting): a body passes through untouched and compares equal.
    Typing full JSON Schema is explicitly out of scope for this kit.

    ``extra="forbid"``: an unexpected key is a validation error, not a field
    carried along silently. A misspelt provider key must fail loudly rather than
    vanish from the payload the model eventually sees.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=_empty_object_schema)
    title: str | None = None
    output_schema: dict | None = None

    # -- construction from the stored shape (R3) --------------------------------

    @classmethod
    def from_openai(cls, d: dict) -> "ToolSpec":
        """Build a ToolSpec from a stored/langchain-produced OpenAI function dict.

        The DB stores what ``convert_to_openai_function`` produces:
        ``{"name", "description", "parameters"}``. Only the argument-schema key
        differs from the canonical model, so this renames ``parameters`` ->
        ``input_schema`` and lets the model's own defaults absorb a missing
        ``description`` (-> ``""``) or a missing ``parameters`` (-> the
        empty-object schema).

        The rename happens *before* construction, deliberately. The model is
        ``extra="forbid"``, so handing the incoming dict through unchanged would
        make every stored spec a validation error on its own ``parameters`` key.
        Equally deliberately, unrecognized keys are NOT dropped: they are passed
        on and rejected, so a misspelt or unsupported provider key (``strict``,
        say) fails loudly instead of vanishing.

        Rejecting loudly is only useful if the noise says something. This runs on
        the live chat path -- ``get_tools`` calls it for every spec of every
        selected toolkit -- so an unreadable stored row surfaces to a user
        mid-conversation. Bare pydantic would report ``Extra inputs are not
        permitted`` and name neither the tool nor the row, leaving an operator to
        work backwards from a stack trace. ``ToolSpecError`` names the offending
        key, what is allowed, and the fix.

        The exposure is narrow but real: ``convert_to_openai_function``
        (langchain-core 0.3.63) emits exactly the three supported keys, so
        anything the current producer writes reads back cleanly. What might not
        is a row persisted by an older version of this two-year-old fork, or a
        row written after a langchain bump starts emitting a fourth key --
        ``strict`` being the obvious candidate, since both OpenAI and Anthropic
        define it and this kit parks it explicitly.
        """
        mapped = {key: value for key, value in d.items() if key != "parameters"}
        if "parameters" in d:
            mapped["input_schema"] = d["parameters"]
        unknown = sorted(set(mapped) - _STORED_KEYS)
        if unknown:
            raise ToolSpecError(
                f"stored tool spec {d.get('name', '<unnamed>')!r} carries "
                f"unsupported key{'s' if len(unknown) > 1 else ''} "
                f"{', '.join(repr(k) for k in unknown)}. A stored spec may only "
                f"contain 'name', 'description', and 'parameters'. Re-saving the "
                f"tool regenerates its specs in the supported shape."
            )
        return cls(**mapped)

    # -- serializers to the three wire formats (R2) -----------------------------
    #
    # Each serializer deep-copies the schema bodies it emits. The output is a
    # detached wire payload: nothing reachable from it is reachable from the
    # model. This is the same hazard R4/T-007 exists to fix -- a spec read off a
    # cached DB model is shared structure, and handing its schema dict onward
    # lets any downstream edit land back on the cache. Sharing the object would
    # be cheaper, but the schemas are small and the cost sits next to an LLM
    # call. Copying is the explicit choice, not an accident of implementation.

    def to_openai(self) -> dict:
        """Serialize to the OpenAI function shape: ``{name, description, parameters}``.

        All three keys are always emitted. ``title``/``output_schema`` are not:
        the OpenAI function object defines no such fields. ``strict`` is parked
        (see the kit's Out of Scope) and is likewise never emitted.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(self.input_schema),
        }

    def to_anthropic(self) -> dict:
        """Serialize to the Anthropic tool shape: ``{name, description, input_schema}``.

        All three keys are always emitted. Anthropic's tool object defines no
        display name and no result schema, so ``title``/``output_schema`` are
        dropped here as they are for OpenAI.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
        }

    def to_mcp(self) -> dict:
        """Serialize to the MCP tool shape (camelCase keys).

        ``{name, description, inputSchema}`` always; ``title`` only when set and
        ``outputSchema`` only when ``output_schema`` is set. Both are omitted
        rather than emitted as ``null`` -- MCP declares them optional, and a
        present-but-null field is a different statement from an absent one.

        This is the only place in the model where camelCase appears. The
        canonical field names stay snake_case; the wire rename happens at the
        edge and nowhere else.
        """
        payload = {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input_schema),
        }
        if self.title is not None:
            payload["title"] = self.title
        if self.output_schema is not None:
            payload["outputSchema"] = copy.deepcopy(self.output_schema)
        return payload

    # -- internal-parameter strip (R4 criterion 1) ------------------------------

    def without_internal_params(self) -> "ToolSpec":
        """Return a copy with ``__``-prefixed argument properties removed.

        Tool functions take injected arguments (``__user__``, ``__id__``,
        ``__event_emitter__``) that the model must never be shown. This replaces
        the in-place ``spec["parameters"]["properties"] = {...}`` write at
        ``utils/tools.py:62-64``, which mutated a spec object read off a cached
        ``Tools`` DB model -- shared structure, so the strip leaked into every
        later reader of that row. The receiver here is left untouched and the
        returned copy shares no dict with it.

        ``required``: verified, not assumed. ``get_tools_specs``
        (``utils/tools.py:195``) feeds each function through
        ``function_to_pydantic_model`` -> ``create_model``, and pydantic v2
        treats a leading-underscore name as a private attribute, so a
        ``__``-prefixed argument never reaches the generated model at all --
        it appears in neither ``properties`` nor ``required`` of the resulting
        schema (confirmed by running ``get_tools_specs`` over a fixture class
        with required ``__id__``/``__event_emitter__``/``__messages__`` args).
        So the live producer cannot emit one. ``required`` is still filtered
        here because this method also runs over specs persisted by older
        versions and over hand-authored ones, where a ``required`` entry naming
        a property we have just removed would leave an invalid JSON Schema on
        the wire.
        """
        schema = copy.deepcopy(self.input_schema)

        properties = schema.get("properties")
        if isinstance(properties, dict):
            schema["properties"] = {key: value for key, value in properties.items() if not _is_internal(key)}

        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [entry for entry in required if not _is_internal(entry)]

        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=schema,
            title=self.title,
            output_schema=copy.deepcopy(self.output_schema),
        )

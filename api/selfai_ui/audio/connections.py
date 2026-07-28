"""Typed audio connection domain — the type-first chooser contract.

cavekit-audio-connections.md R1 (Typed Connection Creation).

Today audio backends live on a separate settings tab as bare free-text fields
(``AUDIO_STT_ENGINE`` / ``AUDIO_TTS_ENGINE`` and their type-specific siblings in
``config.py``), disconnected from the typed, multi-endpoint connection pattern the
platform already uses for its text-model backends (``OLLAMA_API_CONFIGS`` /
``OPENAI_API_CONFIGS`` in ``routers/ollama.py`` / ``routers/openai.py``). This
module brings audio backends into that same established pattern at its first step:
a connection is *created by choosing its type first*, and only then are the fields
that type needs presented.

Five backend types are in scope, mapped from the legacy flat ``*_ENGINE`` strings
the audio router already branches on (see ``routers/audio.py``):

===========================  ==============  ===========  =====================
Typed connection             Capability      Self-hosted  Legacy engine string
===========================  ==============  ===========  =====================
SELF_HOSTED_STT              STT             yes          ``""`` (local whisper /
                                                          self.transcribe)
SELF_HOSTED_TTS              TTS             yes          ``"transformers"``
                                                          (self.speak)
HOSTED_GENERAL               STT + TTS       no           ``"openai"``
HOSTED_SPECIALIZED_A         TTS             no           ``"elevenlabs"``
HOSTED_SPECIALIZED_B         TTS             no           ``"azure"``
===========================  ==============  ===========  =====================

Type-first chooser flow (T-001, R1): present the five types, refuse to expose any
type-specific field until a type is chosen, and let each of the five be selected
individually — after which the field set for *that type only* appears.

Type-appropriate field sets (T-004, R3): ``TYPE_FIELDS`` below gives each type the
fields it — and only it — needs. A field a given type does not use is simply
absent from that type's tuple (not shown-but-disabled). These are inherited
UNCHANGED from the prior settings-tab arrangement: each ``AudioConnectionField``
records the exact ``config.py`` ``PersistentConfig`` it relocates
(``legacy_config``), so the move is field-for-field traceable, not a redesign of
which fields each backend needs. The only omission is the old ``ENGINE`` knob —
the connection *type* now IS the engine (see the mapping table above).

Deliberately NOT here (owned by later tasks against this same kit):
  * multiple connections per type / persistence — T-002 (R2).
  * the self-hosted management entry point — T-005 (R4).
  * migration of already-configured connections — T-006 (R5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class AudioConnectionType(str, Enum):
    """The five supported audio backend connection types (R1).

    ``str``-valued so the member value is directly usable as a stable wire
    identifier without a separate serialization step.
    """

    SELF_HOSTED_STT = "self_hosted_stt"
    SELF_HOSTED_TTS = "self_hosted_tts"
    HOSTED_GENERAL = "hosted_general"
    HOSTED_SPECIALIZED_A = "hosted_specialized_a"
    HOSTED_SPECIALIZED_B = "hosted_specialized_b"


@dataclass(frozen=True)
class AudioConnectionTypeInfo:
    """Chooser-facing descriptor for one connection type.

    This is what the type-first chooser renders — one entry per type, shown
    *before* any type-specific field. ``self_hosted`` and ``capabilities`` are
    carried now because later tasks (T-005 management entry point, voice/model
    catalogs) key off them; they are metadata, not fields the operator edits.
    ``legacy_engine`` records the flat ``*_ENGINE`` string this type replaces so
    migration (T-006) has an unambiguous anchor.
    """

    type: AudioConnectionType
    label: str
    self_hosted: bool
    capabilities: tuple[str, ...]
    legacy_engine: str


# Ordered so the chooser presents a stable list. Order: the two self-hosted types
# first, then the three hosted providers (general-purpose, specialized A, B).
_TYPE_INFO: dict[AudioConnectionType, AudioConnectionTypeInfo] = {
    AudioConnectionType.SELF_HOSTED_STT: AudioConnectionTypeInfo(
        type=AudioConnectionType.SELF_HOSTED_STT,
        label="Self-hosted STT",
        self_hosted=True,
        capabilities=("stt",),
        legacy_engine="",
    ),
    AudioConnectionType.SELF_HOSTED_TTS: AudioConnectionTypeInfo(
        type=AudioConnectionType.SELF_HOSTED_TTS,
        label="Self-hosted TTS",
        self_hosted=True,
        capabilities=("tts",),
        legacy_engine="transformers",
    ),
    AudioConnectionType.HOSTED_GENERAL: AudioConnectionTypeInfo(
        type=AudioConnectionType.HOSTED_GENERAL,
        label="Hosted general-purpose provider",
        self_hosted=False,
        capabilities=("stt", "tts"),
        legacy_engine="openai",
    ),
    AudioConnectionType.HOSTED_SPECIALIZED_A: AudioConnectionTypeInfo(
        type=AudioConnectionType.HOSTED_SPECIALIZED_A,
        label="Hosted specialized provider A",
        self_hosted=False,
        capabilities=("tts",),
        legacy_engine="elevenlabs",
    ),
    AudioConnectionType.HOSTED_SPECIALIZED_B: AudioConnectionTypeInfo(
        type=AudioConnectionType.HOSTED_SPECIALIZED_B,
        label="Hosted specialized provider B",
        self_hosted=False,
        capabilities=("tts",),
        legacy_engine="azure",
    ),
}


@dataclass(frozen=True)
class AudioConnectionField:
    """One editable field in a connection type's form (R3).

    Each field is a *relocation* of a knob that already existed on the prior
    settings-tab arrangement: ``legacy_config`` names the exact
    ``PersistentConfig`` in ``config.py`` this field carries over, so the move is
    field-for-field traceable and nothing about which fields a type needs is
    redesigned. ``secret`` marks a credential a form should mask; ``label`` is the
    human-facing name inherited from the prior tab.
    """

    name: str
    label: str
    legacy_config: str
    secret: bool = False


# Authoritative per-type field sets (R3). Each type collects ONLY the config
# fields its engine branch in ``routers/audio.py`` actually reads today; a field a
# type does not use is simply absent from its tuple. Inherited UNCHANGED from the
# prior settings-tab arrangement — the ``ENGINE`` knob is the sole omission,
# because the connection *type* now IS the engine.
TYPE_FIELDS: dict[AudioConnectionType, tuple[AudioConnectionField, ...]] = {
    # Self-hosted STT (self.transcribe): OpenAI-compatible serving endpoint +
    # T-020's control/management port + model. Self-hosted and perimeter-trusted,
    # so no API key (cavekit R8).
    AudioConnectionType.SELF_HOSTED_STT: (
        AudioConnectionField("base_url", "Base URL", "AUDIO_STT_OPENAI_API_BASE_URL"),
        AudioConnectionField("control_base_url", "Control URL", "AUDIO_STT_CONTROL_BASE_URL"),
        AudioConnectionField("model", "Model", "AUDIO_STT_MODEL"),
    ),
    # Self-hosted TTS (self.speak / transformers): local speaker-model synthesis.
    # Reads only the speaker model + the shared TTS text-splitting knob; no URL or
    # key (embedded, perimeter-trusted).
    AudioConnectionType.SELF_HOSTED_TTS: (
        AudioConnectionField("model", "Speaker Model", "AUDIO_TTS_MODEL"),
        AudioConnectionField("split_on", "Split On", "AUDIO_TTS_SPLIT_ON"),
    ),
    # Hosted general-purpose provider (OpenAI): does BOTH STT and TTS, and the
    # prior arrangement kept those as independent endpoints/keys/models — carried
    # over unchanged (not merged into one endpoint).
    AudioConnectionType.HOSTED_GENERAL: (
        AudioConnectionField("stt_base_url", "STT Base URL", "AUDIO_STT_OPENAI_API_BASE_URL"),
        AudioConnectionField("stt_api_key", "STT API Key", "AUDIO_STT_OPENAI_API_KEY", secret=True),
        AudioConnectionField("stt_model", "STT Model", "AUDIO_STT_MODEL"),
        AudioConnectionField("tts_base_url", "TTS Base URL", "AUDIO_TTS_OPENAI_API_BASE_URL"),
        AudioConnectionField("tts_api_key", "TTS API Key", "AUDIO_TTS_OPENAI_API_KEY", secret=True),
        AudioConnectionField("tts_model", "TTS Model", "AUDIO_TTS_MODEL"),
        AudioConnectionField("tts_voice", "TTS Voice", "AUDIO_TTS_VOICE"),
        AudioConnectionField("split_on", "Split On", "AUDIO_TTS_SPLIT_ON"),
    ),
    # Hosted specialized provider A (ElevenLabs): TTS only, one provider API key.
    # Endpoint is fixed in the engine branch, so there is no base-URL field.
    AudioConnectionType.HOSTED_SPECIALIZED_A: (
        AudioConnectionField("api_key", "API Key", "AUDIO_TTS_API_KEY", secret=True),
        AudioConnectionField("model", "Model", "AUDIO_TTS_MODEL"),
        AudioConnectionField("voice", "Voice", "AUDIO_TTS_VOICE"),
        AudioConnectionField("split_on", "Split On", "AUDIO_TTS_SPLIT_ON"),
    ),
    # Hosted specialized provider B (Azure): TTS only; a region-scoped endpoint and
    # an output-format knob neither other provider uses.
    AudioConnectionType.HOSTED_SPECIALIZED_B: (
        AudioConnectionField("api_key", "API Key", "AUDIO_TTS_API_KEY", secret=True),
        AudioConnectionField("region", "Speech Region", "AUDIO_TTS_AZURE_SPEECH_REGION"),
        AudioConnectionField("voice", "Voice", "AUDIO_TTS_VOICE"),
        AudioConnectionField("output_format", "Output Format", "AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT"),
        AudioConnectionField("split_on", "Split On", "AUDIO_TTS_SPLIT_ON"),
    ),
}


# Name-only view of TYPE_FIELDS, in declaration order — the direct successor to
# T-001's stub. Used where only field identity matters (draft editability and
# membership checks).
TYPE_FIELD_NAMES: dict[AudioConnectionType, tuple[str, ...]] = {
    conn_type: tuple(f.name for f in conn_fields)
    for conn_type, conn_fields in TYPE_FIELDS.items()
}


def fields_for(connection_type: AudioConnectionType) -> tuple[AudioConnectionField, ...]:
    """Return the ordered field descriptors a given type presents (R3).

    Only the chosen type's own fields are returned; a field another type uses is
    absent here, not disabled.
    """

    return TYPE_FIELDS[connection_type]


class TypeNotChosenError(Exception):
    """Raised when a type-specific field is touched before a type is chosen.

    This is the enforcement point for R1's "no type-specific field is editable
    until a type has been chosen" — it is not possible to set a field on a draft
    whose ``type`` is still ``None``.
    """


def available_connection_types() -> list[AudioConnectionTypeInfo]:
    """Return the five selectable types the chooser presents (R1).

    This is the payload rendered *before* any type-specific field. Each of the
    five types appears exactly once and is individually selectable via its
    ``.type``.
    """

    return list(_TYPE_INFO.values())


def get_type_info(connection_type: AudioConnectionType) -> AudioConnectionTypeInfo:
    """Return the descriptor for a single connection type."""

    return _TYPE_INFO[connection_type]


@dataclass
class AudioConnectionDraft:
    """A new-audio-connection draft as it moves through the type-first flow.

    A freshly-created draft has ``type is None`` and therefore exposes no
    editable type-specific fields. Choosing a type is the only way to make any
    field editable, which is exactly R1's contract.
    """

    type: Optional[AudioConnectionType] = None
    fields: dict[str, object] = field(default_factory=dict)

    @property
    def type_chosen(self) -> bool:
        return self.type is not None

    def editable_fields(self) -> tuple[str, ...]:
        """Field names editable in the current draft state.

        Empty until a type is chosen (R1: nothing editable pre-choice); after a
        choice, only the chosen type's (stub) fields.
        """

        if self.type is None:
            return ()
        return TYPE_FIELD_NAMES[self.type]

    def set_field(self, name: str, value: object) -> None:
        """Set a type-specific field value.

        Refuses (``TypeNotChosenError``) while no type is chosen, and refuses
        (``KeyError``) a field name not belonging to the chosen type — so a draft
        can never carry a field from a type other than its own.
        """

        if self.type is None:
            raise TypeNotChosenError(
                "cannot set a type-specific field before a connection type is chosen"
            )
        if name not in TYPE_FIELD_NAMES[self.type]:
            raise KeyError(
                f"field {name!r} is not a field of connection type {self.type.value!r}"
            )
        self.fields[name] = value


def new_audio_connection_draft() -> AudioConnectionDraft:
    """Begin creating a new audio connection.

    Returns a draft with no type chosen and no editable fields — the caller must
    choose one of :func:`available_connection_types` first (R1).
    """

    return AudioConnectionDraft()


def choose_type(
    draft: AudioConnectionDraft, connection_type: AudioConnectionType
) -> AudioConnectionDraft:
    """Choose the draft's type, unlocking that type's fields (R1, R3).

    Any of the five types is individually selectable. Re-choosing a type before
    saving resets the draft's collected fields and re-scopes the editable set to
    the newly chosen type's fields (R3: switching the type updates the presented
    field set), so a draft can never carry a stale field from a previously chosen
    type.
    """

    if not isinstance(connection_type, AudioConnectionType):
        raise TypeError(
            f"connection_type must be an AudioConnectionType, got {type(connection_type)!r}"
        )
    draft.type = connection_type
    draft.fields = {}
    return draft


# ---------------------------------------------------------------------------
# R4: Management action for self-hosted types (T-005)
# ---------------------------------------------------------------------------
#
# A self-hosted STT or self-hosted TTS connection exposes a *management action*
# from its entry on the Connections surface — an in-surface entry point into the
# view where its models (STT) or voices (TTS) are browsed and curated. The three
# hosted-provider types expose no such action.
#
# This repo is the FastAPI API server only; the SvelteKit Connections surface
# lives in the separate ``self.chat`` repo. So this task delivers the *contract*
# the surface renders against, not a widget: a serialized descriptor on the
# connection type that says (a) whether a management action exists, (b) what it
# manages, and (c) where its content is served from — plus the invariant that
# opening it does not navigate away from the Connections surface.
#
# The entry point is type-gated purely off the already-existing
# ``AudioConnectionTypeInfo.self_hosted`` flag (carried by T-001), so the gate is
# a single source of truth: self-hosted -> has action, hosted -> none.
#
# Scope boundary: this task owns only that the entry point EXISTS, is gated
# correctly, and implies no navigation. The CONTENT behind each target is owned
# elsewhere — STT models by ``routers/transcribe.py`` (transcribe-router kit,
# already landed T-020); TTS voices by the voice-catalog kit (T-010). The
# STT-download-vs-TTS-browse distinction between the two views is T-008 (next
# tier), which blocks on this task.


class ManagementActionKind(str, Enum):
    """What a self-hosted connection's management action curates.

    ``str``-valued so the member value is a stable wire identifier, matching the
    convention of :class:`AudioConnectionType`.
    """

    STT_MODELS = "stt_models"
    TTS_VOICES = "tts_voices"


# Where each self-hosted type's management view sources its catalog from. These
# are the anchors the in-surface management view reads — NOT navigation targets
# (see ``opens_in_surface`` on :class:`ManagementAction`).
#
#   * STT models  -> the already-landed transcribe-router listing (T-020),
#     mounted at ``/api/v1/transcribe`` (see ``main.py`` / ``routers/transcribe.py``).
#   * TTS voices  -> the voice catalog owned by T-010 (voice-catalog kit). The
#     concrete route is that task's to finalize; this is the forward-pointer
#     stub, following the ``/api/v1/audio`` prefix the audio router already uses.
_STT_MANAGEMENT_TARGET = "/api/v1/transcribe/models"
_TTS_MANAGEMENT_TARGET = "/api/v1/audio/voices"  # stub: content owned by T-010

# R7 (T-008): the download capability that distinguishes the two self-hosted
# management views. The STT view can pull a model not currently present; the TTS
# view cannot (its voice set is fixed at deploy time — nothing to download).
#
# ``_STT_DOWNLOAD_TARGET`` names the already-landed transcribe-router pull
# endpoint (T-021), a template with a ``{model_id}`` slot the caller fills for
# the model it wants to download:
#     POST /api/v1/transcribe/models/{model_id}/pull
# (mounted in ``main.py`` at prefix ``/api/v1/transcribe``; route declared as
# ``POST /models/{model_id:path}/pull`` in ``routers/transcribe.py``).
# The TTS view has no such endpoint by design (``routers/voice_catalog.py`` is
# GET-only), so its download target is ``None``.
_STT_DOWNLOAD_TARGET = "/api/v1/transcribe/models/{model_id}/pull"

# R8 (T-009): auth-ready credential slot on mutating management requests.
#
# The self-hosted STT management view's *mutating* action (the model download,
# R7/T-008) ships WITHOUT a dedicated authentication mechanism of its own for
# now — it is protected at the network perimeter, not exposed externally — while
# a separate platform-wide authenticated-access effort proceeds independently
# (see ``cavekit-audio-transcribe-router.md`` R6, built as T-027). So a proper
# auth gate can be added later WITHOUT a breaking change, the concrete extension
# point is a single named slot in the mutating request contract:
# ``CREDENTIAL_SLOT`` is the wire key of an OPTIONAL caller credential that is
# present in the request contract but entirely unused and unchecked today. A
# future gate reads this slot without altering the request's existing shape.
CREDENTIAL_SLOT = "credential"

# R8 AC4: the perimeter-posture statement, stated plainly and carried in-code so
# the rendering surface (and any auditor) reads it from the contract itself, not
# only from prose in the kit. These mutating actions rely on network-perimeter
# protection until the separate platform-wide auth effort lands; the credential
# slot is a forward-compatible placeholder, NOT an active check today.
PERIMETER_POSTURE = (
    "Self-hosted STT mutating management actions ship without a dedicated auth "
    "mechanism of their own; they rely on network-perimeter protection (reached "
    "only from within the trusted network, never exposed on an untrusted "
    "external network) until the separate platform-wide authenticated-access "
    "effort lands. The optional credential slot in each mutating request is an "
    "accepted-but-unchecked extension point for that future gate, not an active "
    "authentication check today."
)


@dataclass(frozen=True)
class ManagementAction:
    """The self-hosted management entry point exposed on a connection's entry (R4).

    Present only on self-hosted STT/TTS connections. It is an *in-surface* action:
    ``opens_in_surface`` is always ``True``, encoding R4's "opening the management
    action does not navigate away from the Connections surface" as a contract
    invariant the rendering surface can rely on rather than re-derive.

    ``target`` is where the management view sources its catalog content from — it
    is an embedded-content anchor, not a page to navigate the browser to.

    ``supports_download`` / ``download_target`` are the R7 capability contract
    (T-008): they make the STT-vs-TTS difference an *explicit, discoverable*
    field on the action rather than something a caller infers by knowing which
    ``kind`` maps to which router. The self-hosted STT view can download a model
    not currently present, so it carries ``supports_download=True`` and a
    non-empty ``download_target`` naming the pull endpoint; the self-hosted TTS
    view is browse-and-curate only (its voice set is fixed at deploy time), so it
    carries ``supports_download=False`` and ``download_target=None``. This one
    field is the *only* presentational difference between the two views (R7 AC4):
    both are the same ``ManagementAction`` shape, reached through the same
    ``management_action_for_type`` entry point, presenting the same list-based
    catalog.

    ``accepts_credential`` / ``credential_slot`` are the R8 auth-readiness
    contract (T-009): a *mutating* management action (today: only the STT
    download) declares that its request contract carries an OPTIONAL caller
    credential slot — named by ``credential_slot`` (:data:`CREDENTIAL_SLOT`) —
    which is accepted but wholly unused and unchecked today. This makes the
    action's auth-readiness an explicit, discoverable field the surface can key
    off, rather than something a caller infers. A browse-only action (the TTS
    view) has no mutating request, so ``accepts_credential=False`` and
    ``credential_slot=None``. This tracks ``supports_download`` today (the STT
    download is the sole mutation) but is kept a distinct field so auth-readiness
    stays legible if a non-download mutation is ever added.
    """

    kind: ManagementActionKind
    label: str
    target: str
    opens_in_surface: bool = True
    supports_download: bool = False
    download_target: Optional[str] = None
    accepts_credential: bool = False
    credential_slot: Optional[str] = None


def management_action_for_type(
    info: AudioConnectionTypeInfo,
) -> Optional[ManagementAction]:
    """Return the management action for a connection type, or ``None`` (R4).

    Gated solely off ``info.self_hosted``: the two self-hosted types get an
    action, the three hosted providers get ``None`` (R4: hosted types expose no
    self-hosted management action). STT vs TTS is selected off the type's
    declared ``capabilities`` so the action points at the right catalog.
    """

    if not info.self_hosted:
        return None
    if "stt" in info.capabilities:
        # R7 (T-008): STT can download a not-yet-present model on demand.
        # R8 (T-009): that download is a MUTATING request, so it carries the
        # optional-but-unchecked credential slot as its auth-ready extension point.
        return ManagementAction(
            kind=ManagementActionKind.STT_MODELS,
            label="Manage models",
            target=_STT_MANAGEMENT_TARGET,
            supports_download=True,
            download_target=_STT_DOWNLOAD_TARGET,
            accepts_credential=True,
            credential_slot=CREDENTIAL_SLOT,
        )
    # R7 (T-008): TTS is browse-and-curate only — voice set fixed at deploy time,
    # so no download action (supports_download=False, no download target).
    # R8 (T-009): with no mutating request, there is no credential slot to declare.
    return ManagementAction(
        kind=ManagementActionKind.TTS_VOICES,
        label="Manage voices",
        target=_TTS_MANAGEMENT_TARGET,
        supports_download=False,
        download_target=None,
        accepts_credential=False,
        credential_slot=None,
    )


def has_management_action(info: AudioConnectionTypeInfo) -> bool:
    """Whether a connection type exposes the self-hosted management action (R4).

    True for the two self-hosted types, False for the three hosted providers —
    the boolean form of :func:`management_action_for_type`, mirroring
    ``info.self_hosted`` so callers that only need the yes/no gate need not
    unpack the full descriptor.
    """

    return management_action_for_type(info) is not None


def serialize_management_action(action: Optional[ManagementAction]) -> Optional[dict]:
    """Serialize a management action to the wire, or ``None`` when absent."""

    if action is None:
        return None
    return {
        "kind": action.kind.value,
        "label": action.label,
        "target": action.target,
        "opens_in_surface": action.opens_in_surface,
        # R7 (T-008): the download-capability contract — the one field that
        # differs between the STT (True + target) and TTS (False + None) views.
        "supports_download": action.supports_download,
        "download_target": action.download_target,
        # R8 (T-009): the auth-ready credential-slot contract — whether the
        # action's mutating request accepts an (unused-today) caller credential,
        # and the wire key of that slot. The surface reads these to know where a
        # future auth gate would attach without the request shape changing.
        "accepts_credential": action.accepts_credential,
        "credential_slot": action.credential_slot,
    }


def serialize_connection_field(field: AudioConnectionField) -> dict:
    """Serialize one type-specific field descriptor for a connection form (R3).

    ``secret`` tells the form to mask the value; ``legacy_config`` is deliberately
    NOT exposed — it is an internal migration anchor, not something a form needs.
    """

    return {"name": field.name, "label": field.label, "secret": field.secret}


def serialize_connection_type(info: AudioConnectionTypeInfo) -> dict:
    """Serialize a connection-type descriptor for the Connections surface.

    Carries the chooser-facing metadata, the R4 ``management_action`` field (a
    descriptor on the two self-hosted types and ``None`` on the three hosted
    providers, so the surface renders the management entry point iff one exists —
    without duplicating the self-hosted/hosted gate logic client-side), and the
    R3 ``fields`` list.

    ``fields`` is what makes the type-first chooser possible client-side: the
    surface presents the types, and once one is chosen it renders exactly that
    type's fields — read from this payload rather than hardcoded, so the form can
    never drift from ``TYPE_FIELDS``.
    """

    return {
        "type": info.type.value,
        "label": info.label,
        "self_hosted": info.self_hosted,
        "capabilities": list(info.capabilities),
        "fields": [serialize_connection_field(f) for f in fields_for(info.type)],
        "management_action": serialize_management_action(
            management_action_for_type(info)
        ),
    }


# ---------------------------------------------------------------------------
# R8: Mutating management request contract with an auth-ready credential slot (T-009)
# ---------------------------------------------------------------------------
#
# cavekit-audio-connections.md R8 (Mutating Operations Ship Without Blocking on
# Platform-Wide Auth).
#
# The self-hosted STT connection's mutating management action (the model
# download, R7/T-008) ships now, protected at the network perimeter, without
# waiting on the separate platform-wide authenticated-access effort. To keep that
# future gate a non-breaking addition, every mutating management request defines
# an OPTIONAL credential slot in its request contract — present but unused and
# unchecked today. ``ManagementMutationRequest`` is that contract at the
# connections-domain level: the shape a caller submits to a mutating action's
# ``download_target``. The concrete transcribe-router endpoint wires this slot in
# T-027 (transcribe-router R6); this task owns the domain-level contract and the
# invariant that the slot is accepted-but-unchecked today.
#
# Scope boundary: this task does NOT build the platform-wide auth effort itself,
# and does NOT read/verify the credential. It only guarantees the slot EXISTS in
# the contract, is optional, and is inert today (:attr:`is_authenticated` is
# always ``False``), so a later gate can start reading it without reshaping the
# request. See :data:`PERIMETER_POSTURE` for the R8 AC4 posture statement.


class NotAMutatingActionError(ValueError):
    """Raised when a mutation request is built for a non-mutating action.

    Only a mutating management action (``supports_download=True`` — today, the
    self-hosted STT download) has a mutating request contract. A browse-only
    action (the self-hosted TTS view) has no mutation to authorize, so building a
    :class:`ManagementMutationRequest` for it is a caller error.
    """


@dataclass(frozen=True)
class ManagementMutationRequest:
    """The request contract for a mutating management action, with a credential slot (R8).

    The sole mutating management action in this domain is the self-hosted STT
    model download (R7/T-008). This is the contract a caller submits to that
    action's ``download_target``:

      * ``model_id`` — the operation parameter (which model to download).
      * ``credential`` — the OPTIONAL, accepted-but-UNCHECKED caller credential
        slot (R8 AC3). It is present in the contract so a future platform-wide
        auth gate can read it without changing this request's shape, but nothing
        reads or verifies it today.

    The slot being inert today is encoded as :attr:`is_authenticated`, which is
    always ``False``: no request is authenticated by this contract yet. A future
    gate supplants that property; it does not need to reshape the request. Until
    then these actions rely on network-perimeter protection (:data:`PERIMETER_POSTURE`).
    """

    model_id: str
    credential: Optional[str] = None

    @property
    def is_authenticated(self) -> bool:
        """Whether this request is authenticated — always ``False`` today (R8).

        The credential slot is accepted but unchecked, so no request is
        authenticated by this contract yet. This is the single seam a future
        auth gate replaces, leaving the request's wire shape untouched.
        """

        return False

    def to_serializable(self) -> dict[str, object]:
        """JSON-friendly request body, always including the credential slot (R8 AC3).

        The ``credential`` key is present even when ``None`` so the optional slot
        is a stable part of the wire contract, not something that appears only
        once a credential is supplied.
        """

        return {"model_id": self.model_id, CREDENTIAL_SLOT: self.credential}


def build_mutation_request(
    action: ManagementAction,
    model_id: str,
    credential: Optional[str] = None,
) -> ManagementMutationRequest:
    """Build the request for a mutating management ``action`` (R8).

    Refuses (:class:`NotAMutatingActionError`) a browse-only action, which has no
    mutating request. The ``credential`` is accepted into the contract's optional
    slot but is neither read nor verified here — it is inert until a future auth
    gate reads it (R8 AC3); today the action relies on network-perimeter
    protection (:data:`PERIMETER_POSTURE`, R8 AC1/AC2/AC4).
    """

    if not action.supports_download:
        raise NotAMutatingActionError(
            f"management action {action.kind.value!r} is browse-only and has no "
            "mutating request contract"
        )
    return ManagementMutationRequest(model_id=model_id, credential=credential)


# ---------------------------------------------------------------------------
# Persistence — multiple connections per type, independently addressable (R2).
#
# cavekit-audio-connections.md R2 (Multiple Connections Per Type).
#
# T-001 (above) gave the type-first *creation* flow but held nothing: a saved
# draft had nowhere to live. This layer supplies that store, mirroring the
# text-model multi-connection pattern (``OLLAMA_API_CONFIGS`` /
# ``OPENAI_API_CONFIGS`` in ``config.py`` / ``routers/ollama.py``): a dict of
# named connection configs that persists across reloads.
#
# The one deliberate difference from the text-model store is the key. The Ollama
# store keys its dict by *url*, which means two configs sharing a url would
# collapse into one. Audio connections must let two connections *of the same
# type* — and even the same address — coexist without merging (R2 AC2), so this
# store keys by a **generated stable id** instead. Type is a property of the
# stored value, never the key, so nothing is ever addressed (or overwritten) by
# type alone.
#
# Everything here is pure/std-lib and side-effect-free; the actual durable
# backing is ``config.AUDIO_CONNECTION_CONFIGS`` (a ``PersistentConfig``), to
# which :meth:`AudioConnectionStore.to_serializable` /
# :meth:`AudioConnectionStore.from_serializable` marshal. That round-trip is what
# makes the saved set survive a reload of the Connections surface (R2 AC5).
#
# Out of scope here (later tasks): the reload-survival / edit-delete isolation
# *hardening* pass is T-003; the router endpoints that expose this CRUD are
# wired where the audio Connections surface is assembled.
# ---------------------------------------------------------------------------


class ConnectionNotFoundError(KeyError):
    """Raised when a saved-connection id is not present in the store.

    Distinct type (not a bare ``KeyError``) so callers can tell "no such
    connection id" apart from an internal mapping miss.
    """


class UnknownFieldError(KeyError):
    """Raised when a field name does not belong to a connection's type.

    Mirrors :class:`AudioConnectionDraft.set_field`'s ``KeyError`` guard so a
    persisted connection can never carry a field from a type other than its own.
    """


def _validate_fields(
    connection_type: AudioConnectionType, fields: dict[str, object]
) -> None:
    allowed = TYPE_FIELD_NAMES[connection_type]
    for name in fields:
        if name not in allowed:
            raise UnknownFieldError(
                f"field {name!r} is not a field of connection type "
                f"{connection_type.value!r}"
            )


@dataclass
class SavedAudioConnection:
    """One persisted audio connection: a stable id, its type, and its field values.

    The ``id`` is what makes a connection *independently addressable* (R2 AC2):
    two connections of the same type get two distinct ids and are referenced,
    edited, and deleted individually by id — never merged into a sibling.
    """

    id: str
    type: AudioConnectionType
    fields: dict[str, object] = field(default_factory=dict)

    def to_serializable(self) -> dict[str, object]:
        """Return the JSON-friendly value form (without the id, which is the key).

        Shape: ``{"type": <AudioConnectionType value>, "fields": {...}}`` — the
        value half of the ``config.AUDIO_CONNECTION_CONFIGS`` dict.
        """

        return {"type": self.type.value, "fields": dict(self.fields)}

    @classmethod
    def from_serializable(
        cls, connection_id: str, data: dict[str, object]
    ) -> "SavedAudioConnection":
        """Rebuild a saved connection from its persisted ``{id: value}`` entry."""

        raw_fields = data.get("fields") or {}
        return cls(
            id=connection_id,
            type=AudioConnectionType(data["type"]),
            fields=dict(raw_fields),
        )


# Fields, in priority order, whose value best NAMES a connection for a human
# (cavekit-audio-voice-picker R4 / T-018). ``SavedAudioConnection`` carries no
# dedicated human name field — only an id, a type, and the type's field values —
# so a connection's human-readable label is derived from its type label plus the
# most distinguishing field it holds. For the self-hosted TTS field set (T-004)
# that is ``model`` (the speaker model); the hosted providers name themselves by
# ``tts_model`` / ``voice`` / ``tts_voice``. This is a display-only derivation:
# the connection ``id`` remains the stable, unique identity (R2).
_CONNECTION_LABEL_FIELDS: tuple[str, ...] = (
    "model",
    "tts_model",
    "voice",
    "tts_voice",
)


def connection_display_label(connection: "SavedAudioConnection") -> str:
    """Return a human-readable label for a saved connection (voice-picker R4 / T-018).

    A saved connection has no dedicated human name field, so its label is composed
    from its type's human label (:attr:`AudioConnectionTypeInfo.label`) plus the
    first meaningful field value it carries (``model`` / ``voice`` / …), e.g.
    ``"Self-hosted TTS (glados)"``. When the connection holds no such field the
    bare type label is used. This gives the admin voice-curation surface a
    meaningful source to *show* per voice, rather than the opaque connection id,
    while the id itself stays the authoritative identity.

    Not guaranteed unique on its own — two connections of the same type and model
    share a base label; :func:`selfai_ui.audio.voice_catalog.disambiguate_source_labels`
    is what makes the *shown* source distinct across such connections (R4 AC2).
    """

    type_info = get_type_info(connection.type)
    for key in _CONNECTION_LABEL_FIELDS:
        val = connection.fields.get(key)
        if isinstance(val, str) and val.strip():
            return f"{type_info.label} ({val.strip()})"
    return type_info.label


class AudioConnectionStore:
    """A set of persisted audio connections, keyed by stable id (R2).

    Supports the full independent-CRUD lifecycle — create, read/list, edit,
    delete — with every operation scoped to a single id so acting on one
    connection never touches another of the same (or any other) type.
    Insertion order is preserved so the listing is stable across a reload.
    """

    def __init__(self, id_factory: Optional[Callable[[], str]] = None) -> None:
        # dict preserves insertion order -> stable listing; keyed by id, so two
        # same-type (or same-address) connections never collide.
        self._by_id: dict[str, SavedAudioConnection] = {}
        self._id_factory: Callable[[], str] = id_factory or (lambda: uuid.uuid4().hex)

    # --- create ------------------------------------------------------------

    def add(
        self,
        connection_type: AudioConnectionType,
        fields: Optional[dict[str, object]] = None,
    ) -> SavedAudioConnection:
        """Persist a new connection of ``connection_type`` and return it (R2 AC1).

        A fresh id is generated for every call, so two ``add`` calls with the
        same type (and even identical fields) yield two independently addressable
        connections rather than one overwriting the other.
        """

        if not isinstance(connection_type, AudioConnectionType):
            raise TypeError(
                f"connection_type must be an AudioConnectionType, got "
                f"{type(connection_type)!r}"
            )
        materialized = dict(fields or {})  # copy: caller's dict can't bleed in
        _validate_fields(connection_type, materialized)

        connection_id = self._id_factory()
        if connection_id in self._by_id:
            raise ValueError(f"connection id {connection_id!r} already exists")

        connection = SavedAudioConnection(
            id=connection_id, type=connection_type, fields=materialized
        )
        self._by_id[connection_id] = connection
        return connection

    def save_draft(self, draft: AudioConnectionDraft) -> SavedAudioConnection:
        """Persist a completed :class:`AudioConnectionDraft` from the T-001 flow.

        Bridges the type-first creation flow to storage; refuses a draft whose
        type was never chosen (the same guard T-001 enforces on field edits).
        """

        if draft.type is None:
            raise TypeNotChosenError(
                "cannot save a draft before a connection type is chosen"
            )
        return self.add(draft.type, draft.fields)

    # --- read --------------------------------------------------------------

    def get(self, connection_id: str) -> SavedAudioConnection:
        """Return the connection with ``connection_id`` (independent addressing)."""

        try:
            return self._by_id[connection_id]
        except KeyError:
            raise ConnectionNotFoundError(
                f"no saved audio connection with id {connection_id!r}"
            )

    def list(self) -> list[SavedAudioConnection]:
        """Return the full saved set, in insertion order (R2 AC5).

        Nothing is collapsed or de-duplicated: every persisted connection appears
        exactly once, including multiple of the same type.
        """

        return list(self._by_id.values())

    def list_by_type(
        self, connection_type: AudioConnectionType
    ) -> list[SavedAudioConnection]:
        """Return every saved connection of one type — possibly more than one (R2)."""

        return [c for c in self._by_id.values() if c.type == connection_type]

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, connection_id: object) -> bool:
        return connection_id in self._by_id

    # --- update ------------------------------------------------------------

    def update_fields(
        self, connection_id: str, fields: dict[str, object]
    ) -> SavedAudioConnection:
        """Merge ``fields`` into one connection, addressed by id (R2 AC3).

        Only the connection with ``connection_id`` is touched; any sibling —
        including one of the same type — is left byte-for-byte unchanged, because
        each is a distinct stored instance under its own key.
        """

        connection = self.get(connection_id)
        _validate_fields(connection.type, fields)
        connection.fields.update(fields)
        return connection

    # --- delete ------------------------------------------------------------

    def delete(self, connection_id: str) -> SavedAudioConnection:
        """Remove and return one connection by id (R2 AC4).

        Deleting one connection of a shared type removes only that id; every
        other connection (of the same or a different type) is untouched.
        """

        try:
            return self._by_id.pop(connection_id)
        except KeyError:
            raise ConnectionNotFoundError(
                f"no saved audio connection with id {connection_id!r}"
            )

    # --- persistence round-trip -------------------------------------------

    def to_serializable(self) -> dict[str, dict[str, object]]:
        """Marshal the whole set to the ``config.AUDIO_CONNECTION_CONFIGS`` form.

        ``{id: {"type": ..., "fields": {...}}}`` — insertion order preserved so a
        reload restores the same, unmerged set (R2 AC5).
        """

        return {cid: c.to_serializable() for cid, c in self._by_id.items()}

    @classmethod
    def from_serializable(
        cls,
        data: Optional[dict[str, dict[str, object]]],
        id_factory: Optional[Callable[[], str]] = None,
    ) -> "AudioConnectionStore":
        """Rebuild a store from persisted config (the reverse of the above).

        The exact ids and per-id values are restored verbatim, so nothing is
        silently collapsed or overwritten by another of the same type across a
        reload (R2 AC5).
        """

        store = cls(id_factory=id_factory)
        for connection_id, entry in (data or {}).items():
            store._by_id[connection_id] = SavedAudioConnection.from_serializable(
                connection_id, entry
            )
        return store

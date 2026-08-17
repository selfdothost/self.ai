import logging
import time
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Boolean, Column, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, JSONField, get_db
from selfai_ui.models.users import UserResponse, Users
from selfai_ui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# Models DB Schema
####################


# ModelParams is a model for the data stored in the params field of the Model table
class ModelParams(BaseModel):
    model_config = ConfigDict(extra="allow")
    pass


####################
# Lineage sub-schemas (self.ai#140)
####################

# Every lineage structure below used to be a bare `dict` / `list` whose real
# shape lived only in a comment. Per D2 of gitlab-profile
# `context/treasuremaps/2026-08-13-model-capacity-honesty.md`, a field nothing
# enforces is not a field a consumer can rely on: `capabilities` was declared
# `Optional[dict]` and 97 of 101 live rows carry `null`, which is exactly how a
# downstream guard turns into dead code without anyone noticing.
#
# All of these keep `extra="allow"` for the same reason `ModelMeta` does (see
# the note there): the writers are separate services on their own release
# cadence, and a key llamolotl adds tomorrow must not 422 a model save today.
# The difference from before is that the *declared* keys are now typed and
# validated, so a consumer may depend on them. Extras are tolerated payload,
# never a place to put something a consumer must depend on.


SourceType = Literal[
    "gguf",
    "safetensors_converted",
    "baked",
    "lora_gguf",
    "distilled",
]
"""How the weights on disk came to exist.

Enumerated from the producers, not from the old comment, which was already
stale. self.llamolotl `api/state.py` writes all of these into
`models_meta.json`, and self.ai copies them verbatim in
`routers/llamolotl.register_model`:

* ``gguf``                  — pulled as a GGUF from HuggingFace
  (``_record_model_meta(..., "gguf")``)
* ``safetensors_converted`` — pulled as safetensors and converted locally
* ``baked``                 — a GGUF with LoRAs permanently merged in
  (``routers/pipeline.py``); see :class:`BakeInfo`
* ``lora_gguf``             — a LoRA adapter GGUF (``_record_lora_meta``). Not a
  servable model, but it is a value a live producer emits, and a rejected
  value here is silently swallowed by the ``except`` around the register
  path — the lineage would just vanish. Accepted rather than lost.
* ``distilled``             — self.ai#140: a genuinely smaller set of weights
  produced from a teacher by soft-target distillation; see
  :class:`DistillationInfo`.

Unknown values are rejected. Adding a kind is a schema change on purpose.
"""


class BakedAdapter(BaseModel):
    """One LoRA merged into a baked GGUF."""

    path: str
    weight: float = 1.0

    model_config = ConfigDict(extra="allow")


class BakeInfo(BaseModel):
    """Lineage for a GGUF whose LoRAs were permanently merged in.

    Written by self.llamolotl `routers/pipeline.py` at bake time and copied onto
    the model row by `routers/llamolotl.register_model`.
    """

    base_model: Optional[str] = None
    adapters: list[BakedAdapter] = []
    outtype: Optional[str] = None
    quant_type: Optional[str] = None
    baked_at: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class ActiveLora(BaseModel):
    """One LoRA dynamically applied to llama-server for this model.

    `file`/`scale` is the wire shape both directions: self.chat's ModelEditor
    sends `{file, scale}`, and `routers/llamolotl._ensure_loras_applied` reads
    `lora["file"]` back out when deciding whether a restart is needed.
    """

    file: str
    scale: float = 1.0

    model_config = ConfigDict(extra="allow")


class PruneRecipe(BaseModel):
    """What was removed from the teacher to create the student.

    Recorded as before/after counts rather than a ratio: "30% of depth" is a
    number somebody rounded, "48 layers became 32" is a fact you can check
    against the weights.
    """

    strategy: Literal["depth", "width", "depth_and_width"]

    layers_before: Optional[int] = None
    layers_after: Optional[int] = None

    hidden_size_before: Optional[int] = None
    hidden_size_after: Optional[int] = None

    intermediate_size_before: Optional[int] = None
    intermediate_size_after: Optional[int] = None

    notes: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class DistillationInfo(BaseModel):
    """Lineage for a model produced by distilling a teacher into a student.

    Per D9 of gitlab-profile
    `context/treasuremaps/2026-08-13-crew-model-card-lifecycle.md`: distillation
    means emitting a literal smaller set of weights, Hinton-style against the
    teacher's temperature-softened output distribution. Soft targets require a
    shared tokenizer, so a student is same-family as its teacher **by
    construction** — that is a property of the method, not a rule enforced here.

    The mechanism is prune-then-distil-to-recover: there is no smaller sibling to
    train into, so the student is created by pruning the teacher
    (:class:`PruneRecipe`) and the distillation recovers the lost quality.

    Note the two different corpora this can be confused with:

    * `source_corpus_repo` / `source_corpus_commit_id` — the **text** the
      teacher's forward pass ran over, pinned so the run is reproducible.
    * `ModelMeta.corpus_repo` / `ModelMeta.corpus_commit_id` — where the
      resulting **weights** are catalogued, which is not the same as where the
      bytes are; see `ModelMeta.corpus_weights_present`.
    """

    teacher_model_id: Optional[str] = None
    """The `model.id` of the teacher, when the teacher is served here."""

    teacher_hf_repo: Optional[str] = None
    """The teacher's upstream repo, for a teacher that has no local row."""

    method: Optional[str] = "soft_target_kd"
    """Distillation objective. `soft_target_kd` is KL against softened logits."""

    temperature: Optional[float] = None
    """Softmax temperature the teacher's logits were softened with."""

    top_k_logits: Optional[int] = None
    """k, when the teacher's forward pass was cached as top-k logits to disk."""

    prune_recipe: Optional[PruneRecipe] = None

    source_corpus_repo: Optional[str] = None
    """self.corpus repo holding the text the teacher ran over."""

    source_corpus_commit_id: Optional[str] = None
    """Commit in `source_corpus_repo`. Without it the run is not reproducible."""

    distilled_at: Optional[str] = None
    """ISO-8601, matching `pulled_at` / `baked_at`."""

    model_config = ConfigDict(extra="allow")


# ModelMeta is a model for the data stored in the meta field of the Model table
class ModelMeta(BaseModel):
    """Metadata blob for a `model` row.

    On `extra="allow"` (self.ai#140 decided to **keep** it, deliberately):

    It is not vestigial — it is the extension channel, and it is load-bearing
    today. self.chat's ModelEditor writes `knowledge`, `toolIds`, `filterIds`,
    `actionIds`, `audio`, `tags` and `suggestion_prompts` into this blob, none
    of them declared here; the live table has rows carrying `tags`, `audio` and
    `suggestion_prompts` right now. `extra="ignore"` would silently drop that
    on the next save and `extra="forbid"` would 422 every model save. Mods get
    the same channel by the same mechanism.

    What #140 changes is the *status* of a declared field. The rule, stated so
    the next reader does not have to guess:

    * **Declared fields are authoritative and validated.** Their names are
      reserved and their types are enforced — a consumer may depend on them.
    * **Extras are tolerated payload.** Nothing validates them and nothing
      guarantees them. Anything a consumer must be able to depend on gets
      declared here first; "it can be added without a migration" is the reason
      to declare it, not the reason to skip declaring it.
    """

    profile_image_url: Optional[str] = "/static/favicon.png"

    description: Optional[str] = None
    """
        User-facing description of the model.
    """

    capabilities: Optional[dict] = None

    # --- Lineage: base model provenance ---
    hf_repo: Optional[str] = None
    quant: Optional[str] = None
    source_type: Optional[SourceType] = None
    trainable: Optional[bool] = None
    pulled_at: Optional[str] = None

    # --- Lineage: baked LoRAs (permanently merged) ---
    bake_info: Optional[BakeInfo] = None

    # --- Lineage: active LoRAs (dynamically loaded for testing) ---
    active_loras: Optional[list[ActiveLora]] = None

    # --- Lineage: distillation (self.ai#140) ---
    # Present only on `source_type == "distilled"` rows. Not required by the
    # schema: a row can be marked distilled before the run's details are
    # written back, and a half-recorded distillation is still better read than
    # rejected.
    distillation: Optional[DistillationInfo] = None

    # --- Lineage: model line membership (self.ai#131) ---
    # A pointer, not a copy: the provenance itself lives on the version record
    # in `model_version`. These say *which* version this row is, so the model
    # surface can collapse a line's versions into one entry.
    line_id: Optional[str] = None
    version_id: Optional[str] = None

    # --- Lineage: where the weights live (self.ai#140, consumed by #141) ---
    # The catalogue link self.ai#141 codes against. Named to match
    # `model_versions.ModelLine.corpus_repo` and `ModelVersion.corpus_commit_id`
    # so one concept keeps one name across the three tables that reference it.
    #
    # This is the *weights* repo, not the corpus a distillation trained over —
    # that one is `distillation.source_corpus_repo`.
    #
    # All three are on the row directly rather than reached through
    # `version_id`, because a model can have weights in self.corpus without
    # belonging to a line: #131's lines are opt-in and ENABLE_SELF_CORPUS-gated,
    # and #141 must be able to record and detect a repo/row mismatch either way.
    corpus_repo: Optional[str] = None
    """self.corpus (LakeFS) repository id holding this model's weights.

    Carries the `selfai-*` IAM prefix; see `utils/self_corpus.repo_id_for_*`.
    """

    corpus_commit_id: Optional[str] = None
    """Commit in `corpus_repo` pinning the exact weights this row serves."""

    corpus_weights_present: Optional[bool] = None
    """Whether `corpus_repo` actually holds the weight bytes.

    **A non-null `corpus_commit_id` does not imply the bytes are there**, and
    that is not an edge case — it is the normal state of every backfilled row.
    Core has no filesystem access to `/models`; llamolotl owns that PVC. So
    self.ai#141's backfill creates the repo, commits a catalogue object, and
    links both directions *without ever uploading weights*. That is a real
    commit of a real object. A consumer that read `corpus_repo` and assumed it
    could restore bytes from it would be wrong for every one of those rows.

    Genuinely tri-state, and all three states occur:

    * ``None``  — never recorded.
    * ``True``  — the repo holds the bytes.
    * ``False`` — linked and provenanced, but the bytes live only on the PVC.

    So it does not collapse to a `bool` defaulting False — "we never looked" and
    "we looked and they are not there" are different claims, and defaulting
    would fabricate the second from the first. Nor is it derivable from the
    other two fields, which is exactly why it is declared rather than left to
    `extra="allow"`.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)


class Model(Base):
    __tablename__ = "model"

    id = Column(Text, primary_key=True)
    """
        The model's id as used in the API. If set to an existing model, it will override the model.
    """
    user_id = Column(Text)

    base_model_id = Column(Text, nullable=True)
    """
        An optional pointer to the actual model that should be used when proxying requests.
    """

    name = Column(Text)
    """
        The human-readable display name of the model.
    """

    params = Column(JSONField)
    """
        Holds a JSON encoded blob of parameters, see `ModelParams`.
    """

    meta = Column(JSONField)
    """
        Holds a JSON encoded blob of metadata, see `ModelMeta`.
    """

    access_control = Column(JSON, nullable=True)  # Controls data access levels.
    # Defines access control rules for this entry.
    # - `None`: Public access, available to all users with the "user" role.
    # - `{}`: Private access, restricted exclusively to the owner.
    # - Custom permissions: Specific access control for reading and writing;
    #   Can specify group or user-level restrictions:
    #   {
    #      "read": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      },
    #      "write": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      }
    #   }

    is_active = Column(Boolean, default=True)

    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class ModelModel(BaseModel):
    id: str
    user_id: str
    base_model_id: Optional[str] = None

    name: str
    params: ModelParams
    meta: ModelMeta

    access_control: Optional[dict] = None

    is_active: bool
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class ModelUserResponse(ModelModel):
    user: Optional[UserResponse] = None


class ModelResponse(ModelModel):
    pass


class ModelForm(BaseModel):
    id: str
    base_model_id: Optional[str] = None
    name: str
    meta: ModelMeta
    params: ModelParams
    access_control: Optional[dict] = None
    is_active: bool = True


class ModelsTable:
    def insert_new_model(self, form_data: ModelForm, user_id: str) -> Optional[ModelModel]:
        model = ModelModel(
            **{
                **form_data.model_dump(),
                "user_id": user_id,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        )
        try:
            with get_db() as db:
                result = Model(**model.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)

                if result:
                    return ModelModel.model_validate(result)
                else:
                    return None
        except Exception as e:
            print(e)
            return None

    def get_all_models(self) -> list[ModelModel]:
        with get_db() as db:
            return [ModelModel.model_validate(model) for model in db.query(Model).all()]

    def get_models(self) -> list[ModelUserResponse]:
        with get_db() as db:
            models = []
            for model in db.query(Model).filter(Model.base_model_id.is_not(None)).all():
                user = Users.get_user_by_id(model.user_id)
                models.append(
                    ModelUserResponse.model_validate(
                        {
                            **ModelModel.model_validate(model).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return models

    def get_base_models(self) -> list[ModelModel]:
        with get_db() as db:
            return [
                ModelModel.model_validate(model)
                for model in db.query(Model).filter(Model.base_model_id.is_(None)).all()
            ]

    def get_models_by_user_id(self, user_id: str, permission: str = "write") -> list[ModelUserResponse]:
        models = self.get_models()
        return [
            model
            for model in models
            if model.user_id == user_id or has_access(user_id, permission, model.access_control)
        ]

    def get_model_by_id(self, id: str) -> Optional[ModelModel]:
        try:
            with get_db() as db:
                model = db.get(Model, id)
                return ModelModel.model_validate(model)
        except Exception:
            return None

    def toggle_model_by_id(self, id: str) -> Optional[ModelModel]:
        with get_db() as db:
            try:
                is_active = db.query(Model).filter_by(id=id).first().is_active

                db.query(Model).filter_by(id=id).update(
                    {
                        "is_active": not is_active,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()

                return self.get_model_by_id(id)
            except Exception:
                return None

    def update_model_by_id(self, id: str, model: ModelForm) -> Optional[ModelModel]:
        try:
            with get_db() as db:
                # update only the fields that are present in the model
                db.query(Model).filter_by(id=id).update(model.model_dump(exclude={"id"}))
                db.commit()

                model = db.get(Model, id)
                db.refresh(model)
                return ModelModel.model_validate(model)
        except Exception as e:
            print(e)

            return None

    def delete_model_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Model).filter_by(id=id).delete()
                db.commit()

                return True
        except Exception:
            return False

    def delete_all_models(self) -> bool:
        try:
            with get_db() as db:
                db.query(Model).delete()
                db.commit()

                return True
        except Exception:
            return False


Models = ModelsTable()

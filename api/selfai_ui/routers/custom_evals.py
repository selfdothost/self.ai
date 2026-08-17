"""Admin-registered evaluations: registry, templating and delivery (self.ai#91).

Every row starts `sync_status="pending"` and only a successful push makes it
`synced`, because the API pod cannot write into the harness's storage (all PVCs
are RWO local-path) — registration is genuinely not the same event as the
harness being able to run the task.

Delivery is **manual** (`POST /{id}/sync`), deliberately. A background
reconciler that re-pushes on its own is harder to reason about while a
definition is being edited, and nothing here has been proven end-to-end yet;
retry reliability is worth building after the first working round trip, not
before it.

Templating `hf_task` → task YAML lives in `utils/eval_task_template.py`, in
api-core rather than the client or the harness repos: the whole flow is then
reachable from the API alone, and there is one implementation instead of one per
harness.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from selfai_ui.models.custom_evals import (
    KINDS_BY_EVAL_TYPE,
    SYNC_FAILED,
    SYNC_SYNCED,
    CustomEvalForm,
    CustomEvalModel,
    CustomEvals,
    CustomEvalUpdateForm,
    validate_kind,
    validate_name,
)
from selfai_ui.routers.evaluations import (
    CODE_EVAL_API_URL,
    CODE_EVAL_AUDIENCE,
    LANGUAGE_EVAL_API_URL,
    LANGUAGE_EVAL_AUDIENCE,
    _eval_tasks_cache,
    fetch_harness_tasks,
)
from selfai_ui.utils.auth import get_admin_user
from selfai_ui.utils.eval_task_template import TemplateError, render_definition
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)

router = APIRouter()


async def _reject_if_shadows_builtin(name: str, eval_type: str) -> None:
    """Refuse a name the harness already resolves to a built-in task.

    The harness resolves benchmarks by name, so a shadowing registration would
    silently change what an existing benchmark *means* — and historical results
    carrying that name would then be attributed to the wrong task. Same posture
    `mcp_backends` takes toward GitOps-declared names.

    This deliberately propagates the 502 from `fetch_harness_tasks` rather than
    catching it. If the harness is unreachable we cannot know whether the name
    is taken, and admitting it on that basis is exactly how a shadowing name
    gets in. Failing the registration is recoverable; a silent shadow is not.
    """
    tasks = await fetch_harness_tasks(eval_type)
    builtin = {t.get("name") for t in tasks if isinstance(t, dict)}
    if name in builtin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{name}' is already a built-in {eval_type} task. Registering it would "
                "shadow the built-in and misattribute any results recorded under that name."
            ),
        )


@router.get("", response_model=list[CustomEvalModel])
async def list_custom_evals(eval_type: str | None = None, user=Depends(get_admin_user)):
    if eval_type is not None:
        if eval_type not in KINDS_BY_EVAL_TYPE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown eval_type '{eval_type}' (expected one of: {', '.join(sorted(KINDS_BY_EVAL_TYPE))})",
            )
        return CustomEvals.get_by_eval_type(eval_type)
    return CustomEvals.get_all()


@router.post("", response_model=CustomEvalModel, status_code=status.HTTP_201_CREATED)
async def create_custom_eval(form: CustomEvalForm, user=Depends(get_admin_user)):
    if err := validate_name(form.name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    if err := validate_kind(form.kind, form.eval_type):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    if not form.definition:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="definition must not be empty — it is what the harness is told to run.",
        )

    if CustomEvals.get_by_name(form.name, form.eval_type):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A custom {form.eval_type} evaluation named '{form.name}' already exists.",
        )
    await _reject_if_shadows_builtin(form.name, form.eval_type)

    created = CustomEvals.insert(form, owner_id=user.id, owner_name=getattr(user, "name", None))
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register the evaluation.",
        )
    return created


@router.get("/{id}", response_model=CustomEvalModel)
async def get_custom_eval(id: str, user=Depends(get_admin_user)):
    row = CustomEvals.get_by_id(id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")
    return row


@router.post("/{id}", response_model=CustomEvalModel)
async def update_custom_eval(id: str, form: CustomEvalUpdateForm, user=Depends(get_admin_user)):
    """Update a registration.

    `name`, `eval_type` and `kind` are deliberately not updatable: the harness
    keys its stored copy by name, so a rename is a delete plus a create, not an
    edit. Making it look like an edit would leave an orphan on the harness under
    the old name — precisely the drift `sync_status` exists to expose.
    """
    if not CustomEvals.get_by_id(id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")
    if form.definition is None and form.description is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to update.")
    if form.definition is not None and not form.definition:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="definition must not be empty — it is what the harness is told to run.",
        )

    updated = CustomEvals.update(id, form)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update the evaluation.",
        )
    return updated


@router.delete("/{id}", response_model=bool)
async def delete_custom_eval(id: str, user=Depends(get_admin_user)):
    """Remove a registration, withdrawing it from the harness first.

    Order matters: the harness copy is withdrawn *before* the record is dropped.
    Deleting the record first would leave a task the harness still runs and
    nothing in self.ai remembers — an orphan nobody can find their way back to.
    If the withdrawal fails the record is kept and marked failed, so the
    remaining copy stays visible and retryable rather than becoming invisible.
    """
    row = CustomEvals.get_by_id(id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")

    # Only a row that reached the harness has anything to withdraw.
    if row.eval_type == "code-eval" and row.sync_status == SYNC_SYNCED:
        await _withdraw_code_language(row)
        _eval_tasks_cache.pop(row.eval_type, None)
        return CustomEvals.delete(id)

    if row.eval_type == "language-eval" and row.sync_status == SYNC_SYNCED:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.delete(
                    f"{LANGUAGE_EVAL_API_URL}/api/tasks/{row.name}",
                    headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "tasks:write")},
                )
        except Exception as e:
            log.warning(f"Failed to withdraw custom eval {row.name} from {row.eval_type}: {e}")
            CustomEvals.set_sync_state(id, SYNC_FAILED, f"Could not reach the harness to withdraw: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Could not reach the {row.eval_type} harness to withdraw '{row.name}'. "
                    "The registration was kept so it stays visible and can be retried."
                ),
            )
        # 404 means the harness has no such task — the desired end state, so
        # treat it as success rather than blocking the delete forever.
        if resp.status_code not in (200, 204, 404):
            detail = _harness_detail(resp)
            CustomEvals.set_sync_state(id, SYNC_FAILED, f"Withdrawal refused: {detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The {row.eval_type} harness refused to withdraw '{row.name}': {detail}",
            )
        _eval_tasks_cache.pop(row.eval_type, None)

    return CustomEvals.delete(id)


############################
# HuggingFace inspection
############################

HF_DATASETS_SERVER = "https://datasets-server.huggingface.co"


@router.get("/hf/inspect")
async def inspect_hf_dataset(dataset_path: str, user=Depends(get_admin_user)):
    """Report a dataset's splits and columns so a task definition can be filled in.

    Lives here rather than in the client so the whole add-an-evaluation flow is
    reachable from the API alone. Returns what HuggingFace says; it deliberately
    does not guess a doc_to_text mapping, because a plausible-but-wrong column
    mapping produces a task that scores the wrong thing and still reports a
    number.
    """
    if not dataset_path.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="dataset_path is required")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{HF_DATASETS_SERVER}/info", params={"dataset": dataset_path.strip()})
    except Exception as e:
        log.warning(f"HF datasets-server unreachable for {dataset_path}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the HuggingFace datasets server.",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"HuggingFace returned HTTP {resp.status_code} for dataset '{dataset_path}'.",
        )

    info = resp.json().get("dataset_info", {})
    # dataset_info is either flat or keyed by config name; report every config
    # rather than silently picking one.
    configs = info if isinstance(info, dict) and "features" not in info else {"default": info}
    out = []
    for config_name, cfg in configs.items():
        if not isinstance(cfg, dict):
            continue
        out.append(
            {
                "config": config_name,
                "splits": sorted((cfg.get("splits") or {}).keys()),
                "columns": sorted((cfg.get("features") or {}).keys()),
            }
        )
    return {"dataset_path": dataset_path.strip(), "configs": out}


############################
# Rendering + delivery
############################


def _render_or_400(row: CustomEvalModel) -> str:
    try:
        return render_definition(row.kind, row.name, row.definition, row.description)
    except TemplateError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{id}/rendered")
async def get_rendered_config(id: str, user=Depends(get_admin_user)):
    """The exact YAML the harness would be sent. Reviewable before pushing."""
    row = CustomEvals.get_by_id(id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")
    return {"name": row.name, "eval_type": row.eval_type, "yaml": _render_or_400(row)}


@router.post("/{id}/sync", response_model=CustomEvalModel)
async def sync_custom_eval(id: str, user=Depends(get_admin_user)):
    """Push the definition to its harness and record the outcome.

    Manual by design for now: a background reconciler that re-pushes on its own
    is harder to reason about while a definition is being edited, and nothing
    here has been proven end-to-end yet. Retry reliability comes after the first
    working round trip, not before it.
    """
    row = CustomEvals.get_by_id(id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")

    if row.eval_type == "code-eval":
        return await _sync_code_language(row)

    if row.eval_type != "language-eval":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Delivery for {row.eval_type} is not implemented yet.",
        )

    rendered = _render_or_400(row)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{LANGUAGE_EVAL_API_URL}/api/tasks",
                json={"name": row.name, "yaml_config": rendered},
                headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "tasks:write")},
            )
    except Exception as e:
        log.warning(f"Failed to push custom eval {row.name} to {row.eval_type}: {e}")
        CustomEvals.set_sync_state(id, SYNC_FAILED, f"Could not reach the {row.eval_type} harness: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach the {row.eval_type} harness to deliver the task.",
        )

    if resp.status_code not in (200, 201):
        # Surface the harness's own complaint verbatim — it is the one that
        # knows why (bad YAML, name clash with a built-in, a refused tag).
        detail = _harness_detail(resp)
        CustomEvals.set_sync_state(id, SYNC_FAILED, detail)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The {row.eval_type} harness refused the task: {detail}",
        )

    # The harness's own task list just changed; our cached copy would otherwise
    # hide the new task for up to the TTL.
    _eval_tasks_cache.pop(row.eval_type, None)
    return CustomEvals.set_sync_state(id, SYNC_SYNCED)


async def _withdraw_code_language(row) -> None:
    """Deregister a language from code-eval before its record is dropped.

    Same order and same reasoning as the language-eval withdrawal: a token left
    registered in the harness after self.ai forgets it keeps generating a
    `multiple-{lang}` task nobody can trace back. Kept as a separate function
    because the two harnesses differ on what a missing definition means — here
    the withdrawal target is the language token, not the row's name, and those
    are not the same string.
    """
    token = (row.definition or {}).get("language")
    if not token:
        # Nothing identifiable to withdraw. Deleting the record is still the
        # right end state — there is no token to leave orphaned.
        log.warning(f"code-eval row {row.name} is synced but has no language token to withdraw")
        return

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{CODE_EVAL_API_URL}/api/languages/{token}",
                headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "tasks:write")},
            )
    except Exception as e:
        log.warning(f"Failed to withdraw language {token} from code-eval: {e}")
        CustomEvals.set_sync_state(row.id, SYNC_FAILED, f"Could not reach the harness to withdraw: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Could not reach the code-eval harness to withdraw '{token}'. "
                "The registration was kept so it stays visible and can be retried."
            ),
        )

    # 404: the harness has no such registered language — the desired end state.
    if resp.status_code not in (200, 204, 404):
        detail = _harness_detail(resp)
        CustomEvals.set_sync_state(row.id, SYNC_FAILED, f"Withdrawal refused: {detail}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The code-eval harness refused to withdraw '{token}': {detail}",
        )


async def _sync_code_language(row):
    """Register a MultiPL-E language token with code-eval (self.code-eval#6/#7).

    Deliberately not the same shape as the language-eval push above. code-eval
    takes no task config: a language is a *parameter* of the generated
    `multiple-{lang}` family, so what travels is one token, and the harness — not
    us — decides whether it is honourable. It runs four checks (an `eval_*.py`
    executor exists, MultiPL-E ships a `humaneval-{lang}` dataset config, the
    token maps to a Piston invocable, that invocable is in the live runtime
    list) and refuses with the whole report when any fails.

    That report is passed through verbatim rather than summarised. "no executor"
    and "no dataset config" are different problems with different fixes, and a
    flattened message would send an admin looking in the wrong repo — `python`,
    the case that motivated the dataset check, passes every execution check and
    fails only the dataset one.
    """
    token = (row.definition or {}).get("language")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This code-eval registration has no 'language' in its definition.",
        )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{CODE_EVAL_API_URL}/api/languages",
                json={"language": token},
                headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "tasks:write")},
            )
    except Exception as e:
        log.warning(f"Failed to push custom eval {row.name} to code-eval: {e}")
        CustomEvals.set_sync_state(row.id, SYNC_FAILED, f"Could not reach the code-eval harness: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the code-eval harness to register the language.",
        )

    # 201 on a token already registered too — the harness treats registration as
    # idempotent, so a re-sync after a lost PVC is a repair, not a conflict.
    if resp.status_code not in (200, 201):
        detail = _harness_detail(resp)
        CustomEvals.set_sync_state(row.id, SYNC_FAILED, detail)
        # 503 means the harness could not read its own built-in list, so it
        # refused rather than risk registering a shadow. That is the harness
        # being unavailable for this decision, not the admin asking for
        # something wrong — don't report it as a bad request.
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if resp.status_code == 503
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=code,
            detail=f"The code-eval harness refused '{token}': {detail}",
        )

    _eval_tasks_cache.pop(row.eval_type, None)
    return CustomEvals.set_sync_state(row.id, SYNC_SYNCED)


def _harness_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:
        pass
    return f"HTTP {resp.status_code}"

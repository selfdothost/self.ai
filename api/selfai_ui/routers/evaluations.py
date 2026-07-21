import html
import json
import logging
import math
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.models.eval_jobs import (
    EvalJobForm,
    EvalJobModel,
    EvalJobs,
    EvalJobStatusUpdate,
    EvalJobWithUser,
)
from selfai_ui.models.feedbacks import (
    FeedbackForm,
    FeedbackModel,
    FeedbackResponse,
    Feedbacks,
)
from selfai_ui.models.models import Models
from selfai_ui.models.users import UserModel, Users
from selfai_ui.utils.access_control import has_permission
from selfai_ui.utils.auth import (
    create_eval_token,
    get_admin_user,
    get_verified_user,
    revoke_eval_tokens_for_job,
)
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)

router = APIRouter()

CODE_EVAL_RESULTS_DIR = Path(os.environ.get("CODE_EVAL_RESULTS_DIR", "/app/backend/data/code-eval-results"))

LANGUAGE_EVAL_RESULTS_DIR = Path(os.environ.get("LANGUAGE_EVAL_RESULTS_DIR", "/workspace/results"))

# URL of the code-eval API container
CODE_EVAL_API_URL = os.environ.get("CODE_EVAL_API_URL", "http://self-code-eval:8094")

# Audience strings self.code-eval's/self.language-eval's control APIs
# validate tickets against — must match SERVICE_AUTH_AUDIENCE on each side
# (self.ai#25, the last leg of the service-mesh ticket-auth rollout after
# self.llamolotl, self.curator, and self.transcribe/self.speak).
CODE_EVAL_AUDIENCE = "self.code-eval"
LANGUAGE_EVAL_AUDIENCE = "self.language-eval"

# Test Mode (dry_run) caps every run to a handful of samples so the harness
# returns quickly regardless of the benchmark's real size.
DRY_RUN_SAMPLE_LIMIT = 5

# The completions endpoint code-eval will call for inference.
# Defaults to the UI's own text completions endpoint so requests go through
# the UI's auth, model routing, and access control.
# Use /api/completions (text completion) for code eval — avoids chat formatting
# and thinking-mode issues. Set to /api/chat/completions if needed.
CODE_INFERENCE_API_URL = os.environ.get("CODE_INFERENCE_API_URL", "http://selfai-api:80/api/completions")

# URL of the language-eval API container
LANGUAGE_EVAL_API_URL = os.environ.get("LANGUAGE_EVAL_API_URL", "http://self-language-eval:8096")

# Base URL for language-eval inference (selfUI chat completions endpoint)
LANGUAGE_EVAL_INFERENCE_BASE_URL = os.environ.get(
    "LANGUAGE_EVAL_INFERENCE_BASE_URL", "http://selfai-api:80/api/chat/completions"
)


############################
# GetConfig
############################


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_EVALUATION_ARENA_MODELS": request.app.state.config.ENABLE_EVALUATION_ARENA_MODELS,
        "EVALUATION_ARENA_MODELS": request.app.state.config.EVALUATION_ARENA_MODELS,
    }


############################
# UpdateConfig
############################


class UpdateConfigForm(BaseModel):
    ENABLE_EVALUATION_ARENA_MODELS: Optional[bool] = None
    EVALUATION_ARENA_MODELS: Optional[list[dict]] = None


@router.post("/config")
async def update_config(
    request: Request,
    form_data: UpdateConfigForm,
    user=Depends(get_admin_user),
):
    config = request.app.state.config
    if form_data.ENABLE_EVALUATION_ARENA_MODELS is not None:
        config.ENABLE_EVALUATION_ARENA_MODELS = form_data.ENABLE_EVALUATION_ARENA_MODELS
    if form_data.EVALUATION_ARENA_MODELS is not None:
        config.EVALUATION_ARENA_MODELS = form_data.EVALUATION_ARENA_MODELS
    return {
        "ENABLE_EVALUATION_ARENA_MODELS": config.ENABLE_EVALUATION_ARENA_MODELS,
        "EVALUATION_ARENA_MODELS": config.EVALUATION_ARENA_MODELS,
    }


class FeedbackUserResponse(FeedbackResponse):
    user: Optional[UserModel] = None


@router.get("/feedbacks/all", response_model=list[FeedbackUserResponse])
async def get_all_feedbacks(user=Depends(get_admin_user)):
    feedbacks = Feedbacks.get_all_feedbacks()
    return [
        FeedbackUserResponse(**feedback.model_dump(), user=Users.get_user_by_id(feedback.user_id))
        for feedback in feedbacks
    ]


@router.delete("/feedbacks/all")
async def delete_all_feedbacks(user=Depends(get_admin_user)):
    success = Feedbacks.delete_all_feedbacks()
    return success


@router.get("/feedbacks/all/export", response_model=list[FeedbackModel])
async def export_all_feedbacks(user=Depends(get_admin_user)):
    feedbacks = Feedbacks.get_all_feedbacks()
    return [
        FeedbackModel(**feedback.model_dump(), user=Users.get_user_by_id(feedback.user_id)) for feedback in feedbacks
    ]


@router.get("/feedbacks/user", response_model=list[FeedbackUserResponse])
async def get_feedbacks(user=Depends(get_verified_user)):
    feedbacks = Feedbacks.get_feedbacks_by_user_id(user.id)
    return feedbacks


@router.delete("/feedbacks", response_model=bool)
async def delete_feedbacks(user=Depends(get_verified_user)):
    success = Feedbacks.delete_feedbacks_by_user_id(user.id)
    return success


@router.post("/feedback", response_model=FeedbackModel)
async def create_feedback(
    request: Request,
    form_data: FeedbackForm,
    user=Depends(get_verified_user),
):
    feedback = Feedbacks.insert_new_feedback(user_id=user.id, form_data=form_data)
    if not feedback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(),
        )

    return feedback


@router.get("/feedback/{id}", response_model=FeedbackModel)
async def get_feedback_by_id(id: str, user=Depends(get_verified_user)):
    feedback = Feedbacks.get_feedback_by_id_and_user_id(id=id, user_id=user.id)

    if not feedback:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    return feedback


@router.post("/feedback/{id}", response_model=FeedbackModel)
async def update_feedback_by_id(id: str, form_data: FeedbackForm, user=Depends(get_verified_user)):
    feedback = Feedbacks.update_feedback_by_id_and_user_id(id=id, user_id=user.id, form_data=form_data)

    if not feedback:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    return feedback


@router.delete("/feedback/{id}")
async def delete_feedback_by_id(id: str, user=Depends(get_verified_user)):
    if user.role == "admin":
        success = Feedbacks.delete_feedback_by_id(id=id)
    else:
        success = Feedbacks.delete_feedback_by_id_and_user_id(id=id, user_id=user.id)

    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    return success


############################
# Code Tests (Code Eval)
############################

# Sanitization for model-generated content in evaluation results.
# Model output can contain arbitrary strings (XSS payloads, control chars, etc.)
# that must be neutralized before serving to the UI.

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,127}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_STRING_LEN = 512 * 1024


def _sanitize_string(s: str) -> str:
    s = _CONTROL_CHAR_RE.sub("", s)
    if len(s) > _MAX_STRING_LEN:
        s = s[:_MAX_STRING_LEN] + "\n... [truncated]"
    return html.escape(s, quote=True)


def _sanitize_value(obj: Any) -> Any:
    if isinstance(obj, str):
        return _sanitize_string(obj)
    elif isinstance(obj, dict):
        return {k: _sanitize_value(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_value(item) for item in obj]
    return obj


def _validate_result_id(result_id: str) -> str:
    if not _SAFE_ID_RE.match(result_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid result ID",
        )
    return result_id


def _extract_base_id(results_filename: str) -> str:
    """Extract the base id from a results filename stem (remove '-results' suffix)."""
    stem = Path(results_filename).stem if results_filename.endswith(".json") else results_filename
    if stem.endswith("-results"):
        return stem[: -len("-results")]
    return stem


def _list_result_files() -> list[dict]:
    """Scan the code-eval results directory for *-results.json summary files."""
    results = []
    if not CODE_EVAL_RESULTS_DIR.is_dir():
        return results

    for path in sorted(CODE_EVAL_RESULTS_DIR.glob("*-results.json")):
        try:
            data = json.loads(path.read_text())
            config = data.get("config", {})
            model_name = config.get("model", path.stem)
            tasks = config.get("tasks", "unknown")
            base_id = _extract_base_id(path.name)

            # Try to get created_at from .jobs.json
            created_at = None
            jobs_file = CODE_EVAL_RESULTS_DIR / ".jobs.json"
            if jobs_file.is_file():
                try:
                    jobs = json.loads(jobs_file.read_text())
                    if base_id in jobs:
                        created_at = jobs[base_id].get("created_at")
                except Exception:
                    pass

            # Build a summary entry
            scores = {k: v for k, v in data.items() if k != "config"}
            results.append(
                _sanitize_value(
                    {
                        "id": base_id,
                        "filename": path.name,
                        "model": model_name,
                        "tasks": tasks,
                        "scores": scores,
                        "config": config,
                        "created_at": created_at,
                    }
                )
            )
        except Exception as e:
            log.warning(f"Failed to parse {path}: {e}")

    return results


def _load_details(base_id: str) -> list[dict] | None:
    """Load per-task details for a given result set."""
    if not CODE_EVAL_RESULTS_DIR.is_dir():
        return None

    # Details files: {base_id}-details_{task}_{timestamp}.json or {base_id}-details_{task}.json
    matches = sorted(CODE_EVAL_RESULTS_DIR.glob(f"{base_id}-details_*.json"))
    if not matches:
        # Fallback: try the old pattern
        matches = sorted(CODE_EVAL_RESULTS_DIR.glob(f"{base_id}_*.json"))

    for path in matches:
        # Skip generation files
        if "-generations_" in path.name or path.name.endswith("-results.json"):
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return _sanitize_value(data)
        except Exception as e:
            log.warning(f"Failed to parse details {path}: {e}")

    return None


def _results_present(base_id: str) -> bool:
    """True if per-task details for this run are already persisted locally."""
    if not CODE_EVAL_RESULTS_DIR.is_dir():
        return False
    return bool(
        list(CODE_EVAL_RESULTS_DIR.glob(f"{base_id}-details_*.json"))
        or list(CODE_EVAL_RESULTS_DIR.glob(f"{base_id}_*.json"))
    )


def _compute_quartile_scores(details: list[dict]) -> dict:
    """Compute pass rates for each quartile of tasks."""
    total = len(details)
    if total == 0:
        return {"q1": 0, "q2": 0, "q3": 0, "q4": 0, "total": 0}

    q_size = math.ceil(total / 4)
    quartiles = {}
    for i, label in enumerate(["q1", "q2", "q3", "q4"]):
        start = i * q_size
        end = min(start + q_size, total)
        chunk = details[start:end]
        if chunk:
            passed = sum(1 for t in chunk if all(s.get("passed", False) for s in t.get("samples", [])))
            quartiles[label] = round(passed / len(chunk) * 100, 1)
        else:
            quartiles[label] = 0

    all_passed = sum(1 for t in details if all(s.get("passed", False) for s in t.get("samples", [])))
    quartiles["total"] = round(all_passed / total * 100, 1)
    return quartiles


def _result_ids_owned_by_user(user) -> set[str]:
    """Code-eval result ids a non-admin user is allowed to read.

    Results are keyed on disk by the remote code-eval job id (stashed on the
    EvalJob meta as ``code_eval_job_id`` at dispatch); the schedule path may key
    on the EvalJob id itself. Collect both for every code-eval job the user owns
    so they can see the graded detail for their own runs without exposing anyone
    else's.
    """
    owned: set[str] = set()
    for job in EvalJobs.get_jobs_by_user_id(user.id):
        if job.eval_type != "code-eval":
            continue
        owned.add(job.id)
        code_eval_job_id = (job.meta or {}).get("code_eval_job_id")
        if code_eval_job_id:
            owned.add(str(code_eval_job_id))
    return owned


@router.get("/codetests")
async def get_code_tests(user=Depends(get_verified_user)):
    """List code-eval evaluation result summaries.

    Admins see every run; other users see only results from their own runs so
    the workspace can render the graded pass/fail view instead of falling back
    to raw events.
    """
    results = _list_result_files()
    if user.role == "admin":
        return results
    owned = _result_ids_owned_by_user(user)
    return [r for r in results if r["id"] in owned]


@router.get("/codetests/summary")
async def get_code_tests_summary(user=Depends(get_admin_user)):
    """Aggregate results by model across all benchmarks.

    Returns a list of models with their pass@1 scores per benchmark and average.
    Each task (e.g. apps-introductory, multiple-py) gets its own column.
    """
    all_results = _list_result_files()

    # Group by model: for each model+benchmark, keep the best (latest) score
    model_data: dict[str, dict] = defaultdict(lambda: {"benchmarks": {}, "runs": 0})
    benchmarks_seen: set[str] = set()

    for r in all_results:
        model = r["model"]
        task = r["tasks"]
        benchmarks_seen.add(task)
        model_data[model]["runs"] += 1

        # Extract pass@1 score
        for _task_key, metrics in r["scores"].items():
            if isinstance(metrics, dict) and "pass@1" in metrics:
                score = round(metrics["pass@1"] * 100, 1)
                # Keep the latest (highest id) score per benchmark
                if task not in model_data[model]["benchmarks"] or score > model_data[model]["benchmarks"][task]:
                    model_data[model]["benchmarks"][task] = score

    # Build response
    summary = []
    for model_name, data in sorted(model_data.items()):
        benchmarks = data["benchmarks"]
        scores_list = list(benchmarks.values())
        avg = round(sum(scores_list) / len(scores_list), 1) if scores_list else 0
        summary.append(
            {
                "model": model_name,
                "benchmarks": benchmarks,
                "average": avg,
                "runs": data["runs"],
            }
        )

    return {
        "models": summary,
        "benchmark_names": sorted(benchmarks_seen),
    }


@router.get("/codetests/benchmark/{benchmark_name}")
async def get_code_tests_benchmark(benchmark_name: str, user=Depends(get_admin_user)):
    """Get per-model quartile pass rates for a specific benchmark."""
    if not _SAFE_ID_RE.match(benchmark_name):
        raise HTTPException(status_code=400, detail="Invalid benchmark name")

    all_results = _list_result_files()
    # Filter to this benchmark
    benchmark_results = [r for r in all_results if r["tasks"] == benchmark_name]

    # Group by model, use latest run per model
    model_latest: dict[str, dict] = {}
    for r in benchmark_results:
        model = r["model"]
        if model not in model_latest:
            model_latest[model] = r
        else:
            # Keep the one with the higher id (later run)
            if r["id"] > model_latest[model]["id"]:
                model_latest[model] = r

    rows = []
    for model_name, result in sorted(model_latest.items()):
        details = _load_details(result["id"])
        if details:
            quartiles = _compute_quartile_scores(details)
        else:
            # Fall back to overall score
            score = 0
            for _k, metrics in result["scores"].items():
                if isinstance(metrics, dict) and "pass@1" in metrics:
                    score = round(metrics["pass@1"] * 100, 1)
            quartiles = {
                "q1": score,
                "q2": score,
                "q3": score,
                "q4": score,
                "total": score,
            }

        rows.append(
            {
                "model": model_name,
                "result_id": result["id"],
                **quartiles,
            }
        )

    return {"benchmark": benchmark_name, "rows": rows}


@router.get("/codetests/model-runs/{model_name:path}")
async def get_model_runs(model_name: str, user=Depends(get_admin_user)):
    """List all evaluation runs for a specific model."""
    all_results = _list_result_files()
    runs = [r for r in all_results if r["model"] == model_name]

    # Sort by created_at descending if available, else by id
    runs.sort(key=lambda r: r.get("created_at") or r["id"], reverse=True)

    return runs


@router.get("/codetests/{result_id}")
async def get_code_test_details(result_id: str, user=Depends(get_verified_user)):
    """Get per-task details for a specific evaluation run.

    Admins can read any run; other users only runs they own — this is what lets
    the user workspace show the graded pass/fail task list for its own evals.
    """
    _validate_result_id(result_id)
    if user.role != "admin" and result_id not in _result_ids_owned_by_user(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    details = _load_details(result_id)
    if details is None:
        # Fallback: fetch from code-eval API directly (results not yet synced)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{CODE_EVAL_API_URL}/api/results/{result_id}/details",
                    headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "jobs:read")},
                )
                if resp.status_code == 200:
                    details = _sanitize_value(resp.json())
                    # Persist locally so next request hits disk
                    if details:
                        CODE_EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                        details_path = CODE_EVAL_RESULTS_DIR / f"{result_id}-details_{timestamp}.json"
                        details_path.write_text(json.dumps(details))
        except Exception as e:
            log.warning(f"Failed to fetch details from code-eval for {result_id}: {e}")

    if details is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Details file not found for this evaluation run.",
        )
    return details


############################
# Language Tests (language-eval)
############################


async def _list_language_eval_results() -> list[dict]:
    """List language-eval results.

    Primary source is a live fetch from language-eval's own control-plane API
    (GET /api/results) -- LANGUAGE_EVAL_RESULTS_DIR is never actually mounted
    on this pod (no shared PVC wired into manifests/api/10-deployment.yaml, see
    self.ai#33), so the disk-glob path below is dead in production today. It's
    kept as a first-choice source in case that ever changes, and as a fallback
    if the language-eval pod itself is briefly unreachable.

    Structure (disk path): {LANGUAGE_EVAL_RESULTS_DIR}/{job_id}/{model_name}/results_*.json
    """
    results = []
    seen_language_eval_ids: set[str] = set()

    # Build reverse map: language_eval_job_id -> ui_job_id
    # so results can be linked back to the UI's EvalJob records.
    language_eval_to_ui = {}
    try:
        all_eval_jobs = EvalJobs.get_all_jobs()
        for ej in all_eval_jobs:
            lm_eid = (ej.meta or {}).get("language_eval_job_id")
            if lm_eid:
                language_eval_to_ui[lm_eid] = ej.id
    except Exception:
        pass

    if LANGUAGE_EVAL_RESULTS_DIR.is_dir():
        jobs_file = LANGUAGE_EVAL_RESULTS_DIR / ".jobs.json"
        jobs_meta = {}
        if jobs_file.is_file():
            try:
                jobs_meta = json.loads(jobs_file.read_text())
            except Exception:
                pass

        for results_file in sorted(LANGUAGE_EVAL_RESULTS_DIR.glob("*/*/results_*.json")):
            try:
                data = json.loads(results_file.read_text())
                language_eval_job_id = results_file.parent.parent.name
                # Prefer the UI job ID if we have a mapping
                job_id = language_eval_to_ui.get(language_eval_job_id, language_eval_job_id)
                model_name = data.get("model_name", results_file.parent.name)
                task_results = data.get("results", {})

                # Extract benchmark names and scores
                benchmarks = {}
                for task_name, metrics in task_results.items():
                    # Pick the primary metric for each benchmark
                    score = _extract_language_eval_primary_metric(task_name, metrics)
                    if score is not None:
                        benchmarks[task_name] = round(score * 100, 1)

                created_at = None
                if job_id in jobs_meta:
                    created_at = jobs_meta[job_id].get("created_at")

                n_samples = data.get("n-samples", {})
                total_samples = sum(
                    v.get("effective", v.get("original", 0)) for v in n_samples.values() if isinstance(v, dict)
                )

                results.append(
                    _sanitize_value(
                        {
                            "id": job_id,
                            "language_eval_job_id": language_eval_job_id,
                            "model": model_name,
                            "benchmarks": benchmarks,
                            "total_samples": total_samples,
                            "eval_time": data.get("total_evaluation_time_seconds"),
                            "created_at": created_at,
                            "config": data.get("config", {}),
                            "results_file": str(results_file.relative_to(LANGUAGE_EVAL_RESULTS_DIR)),
                        }
                    )
                )
                seen_language_eval_ids.add(language_eval_job_id)
            except Exception as e:
                log.warning(f"Failed to parse language-eval result {results_file}: {e}")

    # Live fallback: language-eval's own control plane always has these, since
    # it writes results to its own PVC regardless of what this pod can see.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{LANGUAGE_EVAL_API_URL}/api/results",
                headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "jobs:read")},
            )
            resp.raise_for_status()
            for item in resp.json():
                language_eval_job_id = item.get("id") or item.get("job_id")
                if not language_eval_job_id or language_eval_job_id in seen_language_eval_ids:
                    continue
                job_id = language_eval_to_ui.get(language_eval_job_id, language_eval_job_id)
                benchmarks = {}
                for task_name, metrics in (item.get("scores") or {}).items():
                    score = _extract_language_eval_primary_metric(task_name, metrics)
                    if score is not None:
                        benchmarks[task_name] = round(score * 100, 1)

                results.append(
                    _sanitize_value(
                        {
                            "id": job_id,
                            "language_eval_job_id": language_eval_job_id,
                            "model": item.get("model"),
                            "benchmarks": benchmarks,
                            "total_samples": None,
                            "eval_time": None,
                            "created_at": item.get("created_at"),
                            "config": item.get("config", {}),
                            "results_file": None,
                        }
                    )
                )
    except Exception as e:
        log.warning(f"Failed to fetch results from language-eval API: {e}")

    return results


def _extract_language_eval_primary_metric(task_name: str, metrics: dict) -> float | None:
    """Extract the primary accuracy metric from language-eval task results.

    Different benchmarks use different primary metrics. This picks the most
    representative one for display.
    """
    # Priority order of metric keys to try
    priority = [
        "prompt_level_strict_acc,none",
        "exact_match,strict-match",
        "exact_match,flexible-extract",
        "acc_norm,none",
        "acc,none",
        "exact_match,none",
    ]
    for key in priority:
        if key in metrics:
            val = metrics[key]
            if isinstance(val, (int, float)):
                return val
    # Fallback: first numeric value that looks like a score
    for key, val in metrics.items():
        if isinstance(val, (int, float)) and not key.endswith("_stderr,none") and key != "sample_len":
            return val
    return None


async def _load_language_eval_samples(job_id: str, model_name: str) -> list[dict] | None:
    """Load per-sample details from a samples_*.jsonl file.

    Same disk-primary / live-fallback split as _list_language_eval_results --
    see that function's docstring for why the disk path is normally empty.
    """
    if LANGUAGE_EVAL_RESULTS_DIR.is_dir():
        job_dir = LANGUAGE_EVAL_RESULTS_DIR / job_id
        if job_dir.is_dir():
            for samples_file in job_dir.glob("*/samples_*.jsonl"):
                try:
                    samples = []
                    with open(samples_file) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                samples.append(json.loads(line))
                    return _sanitize_value(samples)
                except Exception as e:
                    log.warning(f"Failed to parse samples {samples_file}: {e}")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{LANGUAGE_EVAL_API_URL}/api/results/{job_id}/samples",
                headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "jobs:read")},
            )
            if resp.status_code == 200:
                return _sanitize_value(resp.json())
    except Exception as e:
        log.warning(f"Failed to fetch samples from language-eval for {job_id}: {e}")

    return None


@router.get("/langtests/summary")
async def get_lang_tests_summary(user=Depends(get_admin_user)):
    """Aggregate language-eval results by model across all benchmarks."""
    all_results = await _list_language_eval_results()

    model_data: dict[str, dict] = defaultdict(lambda: {"benchmarks": {}, "runs": 0})
    benchmarks_seen: set[str] = set()

    for r in all_results:
        model = r["model"]
        model_data[model]["runs"] += 1
        for bench_name, score in r["benchmarks"].items():
            benchmarks_seen.add(bench_name)
            # Keep the latest (best) score per benchmark
            if bench_name not in model_data[model]["benchmarks"] or score > model_data[model]["benchmarks"][bench_name]:
                model_data[model]["benchmarks"][bench_name] = score

    summary = []
    for model_name, data in sorted(model_data.items()):
        benchmarks = data["benchmarks"]
        scores_list = list(benchmarks.values())
        avg = round(sum(scores_list) / len(scores_list), 1) if scores_list else 0
        summary.append(
            {
                "model": model_name,
                "benchmarks": benchmarks,
                "average": avg,
                "runs": data["runs"],
            }
        )

    return {
        "models": summary,
        "benchmark_names": sorted(benchmarks_seen),
    }


@router.get("/langtests/model-runs/{model_name:path}")
async def get_lang_model_runs(model_name: str, user=Depends(get_admin_user)):
    """List all language-eval runs for a specific model."""
    all_results = await _list_language_eval_results()
    runs = [r for r in all_results if r["model"] == model_name]
    runs.sort(key=lambda r: r.get("created_at") or r["id"], reverse=True)
    return runs


@router.get("/langtests/{job_id}")
async def get_lang_test_details(job_id: str, user=Depends(get_admin_user)):
    """Get per-sample details for a specific language-eval run."""
    _validate_result_id(job_id)

    # Find the result to get model name
    all_results = await _list_language_eval_results()
    result = next((r for r in all_results if r["id"] == job_id), None)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="language-eval result not found",
        )

    # Use language_eval_job_id for disk path (directory is named by language-eval ID)
    disk_id = result.get("language_eval_job_id", job_id)
    samples = await _load_language_eval_samples(disk_id, result["model"])
    if samples is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Samples file not found for this evaluation run.",
        )

    return {
        "result": result,
        "samples": samples,
    }


############################
# Evaluation Jobs
############################


@router.get("/jobs", response_model=list[EvalJobWithUser])
async def get_eval_jobs(user=Depends(get_verified_user)):
    if user.role == "admin":
        return EvalJobs.get_all_jobs()
    return EvalJobs.get_jobs_by_user_id(user.id)


@router.post("/jobs/create", response_model=Optional[EvalJobWithUser])
async def create_eval_job(
    request: Request,
    form_data: EvalJobForm,
    user=Depends(get_verified_user),
):
    if user.role != "admin" and not has_permission(
        user.id, "workspace.evaluations", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    job = EvalJobs.insert_new_job(user.id, form_data)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Failed to create evaluation job"),
        )

    all_jobs = EvalJobs.get_all_jobs()
    detailed = next((j for j in all_jobs if j.id == job.id), None)
    return detailed or job


@router.post("/jobs/{id}/cancel", response_model=Optional[EvalJobModel])
async def cancel_eval_job(
    id: str,
    user=Depends(get_verified_user),
):
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    if job.status == "cancelled":
        # R2-AC1: cancel-on-cancelled is 2xx no-op
        return job
    if job.status not in ("pending", "scheduled", "queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job cannot be cancelled in its current state",
        )

    # Kill the process on the remote eval container
    if job.status == "running" and job.meta:
        remote_job_id = None
        remote_url = None
        remote_audience = None
        if job.eval_type == "language-eval" and job.meta.get("language_eval_job_id"):
            remote_job_id = job.meta["language_eval_job_id"]
            remote_url = LANGUAGE_EVAL_API_URL
            remote_audience = LANGUAGE_EVAL_AUDIENCE
        elif job.eval_type == "code-eval" and job.meta.get("code_eval_job_id"):
            remote_job_id = job.meta["code_eval_job_id"]
            remote_url = CODE_EVAL_API_URL
            remote_audience = CODE_EVAL_AUDIENCE

        if remote_job_id and remote_url:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.delete(
                        f"{remote_url}/api/jobs/{remote_job_id}",
                        headers={TICKET_HEADER: mint_service_ticket(remote_audience, "jobs:write")},
                    )
                    log.info(f"Cancelled remote {job.eval_type} job {remote_job_id}: " f"{resp.status_code}")
            except Exception as e:
                log.warning(f"Failed to cancel remote job {remote_job_id}: {e}")

    revoke_eval_tokens_for_job(id)
    return EvalJobs.update_job_status(
        id=id,
        update=EvalJobStatusUpdate(status="cancelled"),
    )


@router.delete("/jobs/{id}/delete", response_model=bool)
async def delete_eval_job(id: str, user=Depends(get_admin_user)):
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return EvalJobs.delete_job_by_id(id=id)


async def _dispatch_eval_job(job: EvalJobModel) -> None:
    """Dispatch a single eval job to code-eval.

    Moves the job to 'running' on success or 'failed' on error.
    Called by the background queue processor — not directly by endpoints.
    """
    eval_token = create_eval_token(
        user_id=job.user_id,
        job_id=job.id,
        eval_type="code-eval",
    )

    code_eval_payload = {
        "tasks": job.benchmark,
        "api_endpoint": CODE_INFERENCE_API_URL,
        "model": job.model_id,
        "api_key": eval_token,
    }
    if (job.meta or {}).get("dry_run"):
        # Test Mode: cap samples so large suites (APPS, MultiPL-E) return fast.
        code_eval_payload["limit"] = DRY_RUN_SAMPLE_LIMIT

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{CODE_EVAL_API_URL}/api/jobs",
                json=code_eval_payload,
                headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "jobs:create")},
            )
            resp.raise_for_status()
            code_eval_job = resp.json()
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        log.error(f"code-eval dispatch failed: {error_detail}")
        revoke_eval_tokens_for_job(job.id)
        EvalJobs.update_job_status(
            id=job.id,
            update=EvalJobStatusUpdate(
                status="failed",
                error_message=f"Failed to dispatch to code-eval: {error_detail}",
            ),
        )
        return
    except Exception as e:
        log.error(f"code-eval dispatch error: {e}")
        revoke_eval_tokens_for_job(job.id)
        EvalJobs.update_job_status(
            id=job.id,
            update=EvalJobStatusUpdate(
                status="failed",
                error_message=f"Failed to connect to code-eval: {e}",
            ),
        )
        return

    EvalJobs.update_job_meta(
        id=job.id,
        meta={"code_eval_job_id": code_eval_job.get("job_id")},
    )
    EvalJobs.update_job_status(
        id=job.id,
        update=EvalJobStatusUpdate(status="running"),
    )
    log.info(f"Eval job {job.id} dispatched to code-eval with JIT token")


async def _dispatch_language_eval_job(job: EvalJobModel) -> None:
    """Dispatch a single eval job to language-eval harness.

    Moves the job to 'running' on success or 'failed' on error.
    """
    eval_token = create_eval_token(
        user_id=job.user_id,
        job_id=job.id,
        eval_type="language-eval",
    )

    is_dry_run = (job.meta or {}).get("dry_run", False)

    language_eval_payload = {
        "tasks": job.benchmark,
        "base_url": LANGUAGE_EVAL_INFERENCE_BASE_URL,
        "model": job.model_id,
        "api_key": eval_token,
        "model_type": "local-chat-completions",
        "apply_chat_template": True,
        "log_samples": True,
    }

    if is_dry_run:
        language_eval_payload["dry_run"] = True
        language_eval_payload["dry_run_delay"] = 0.5
        # Fixed small cap — total_samples is the full advertised size (can be
        # tens of thousands), which would defeat the point of a quick test.
        language_eval_payload["limit"] = DRY_RUN_SAMPLE_LIMIT

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{LANGUAGE_EVAL_API_URL}/api/jobs",
                json=language_eval_payload,
                headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "jobs:create")},
            )
            resp.raise_for_status()
            language_eval_job = resp.json()
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        log.error(f"language-eval dispatch failed: {error_detail}")
        revoke_eval_tokens_for_job(job.id)
        EvalJobs.update_job_status(
            id=job.id,
            update=EvalJobStatusUpdate(
                status="failed",
                error_message=f"Failed to dispatch to language-eval: {error_detail}",
            ),
        )
        return
    except Exception as e:
        log.error(f"language-eval dispatch error: {e}")
        revoke_eval_tokens_for_job(job.id)
        EvalJobs.update_job_status(
            id=job.id,
            update=EvalJobStatusUpdate(
                status="failed",
                error_message=f"Failed to connect to language-eval: {e}",
            ),
        )
        return

    # Capture deployment context from the UI's model config
    deployment_context = {
        "language_eval_job_id": language_eval_job.get("job_id"),
        "inference_url": LANGUAGE_EVAL_INFERENCE_BASE_URL,
    }
    model_info = Models.get_model_by_id(job.model_id)
    if model_info:
        params = model_info.params.model_dump() if model_info.params else {}
        meta = model_info.meta.model_dump() if model_info.meta else {}
        deployment_context["model_config"] = {
            "model_id": model_info.id,
            "model_name": model_info.name,
            "base_model_id": model_info.base_model_id,
            "params": params,
            "system_prompt": params.get("system", None),
            "hf_repo": meta.get("hf_repo"),
            "quant": meta.get("quant"),
            "source_type": meta.get("source_type"),
            "active_loras": meta.get("active_loras"),
            "bake_info": meta.get("bake_info"),
        }

    EvalJobs.update_job_meta(id=job.id, meta=deployment_context)
    EvalJobs.update_job_status(
        id=job.id,
        update=EvalJobStatusUpdate(status="running"),
    )
    log.info(f"Eval job {job.id} dispatched to language-eval with JIT token")


async def _fetch_code_eval_results(job: EvalJobModel, code_eval_job_id: str) -> None:
    """Fetch results summary and details from code-eval and persist locally."""
    try:
        CODE_EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Generous timeout: code-eval serves one eval at a time and can be
        # slow to answer while a run is in flight.
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Fetch results summary
            summary_resp = await client.get(
                f"{CODE_EVAL_API_URL}/api/results/{code_eval_job_id}",
                headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "jobs:read")},
            )
            summary_resp.raise_for_status()
            summary = summary_resp.json()

            # Save results summary
            summary_path = CODE_EVAL_RESULTS_DIR / f"{code_eval_job_id}-results.json"
            summary_path.write_text(json.dumps(summary))
            log.info(f"Saved code-eval results summary: {summary_path}")

            # Fetch per-task details
            details_resp = await client.get(
                f"{CODE_EVAL_API_URL}/api/results/{code_eval_job_id}/details",
                headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "jobs:read")},
            )
            if details_resp.status_code == 200:
                details = details_resp.json()
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                details_path = CODE_EVAL_RESULTS_DIR / f"{code_eval_job_id}-details_{timestamp}.json"
                details_path.write_text(json.dumps(details))
                log.info(f"Saved code-eval results details: {details_path}")

    except Exception as e:
        log.error(f"Failed to fetch code-eval results for job {code_eval_job_id}: {e}")


async def _sync_running_jobs() -> None:
    """Check code-eval and language-eval for status updates on all running jobs."""
    running_jobs = EvalJobs.get_jobs_by_status("running")
    if not running_jobs:
        return

    for job in running_jobs:
        eval_type = getattr(job, "eval_type", "code-eval") or "code-eval"

        if eval_type == "language-eval":
            language_eval_job_id = (job.meta or {}).get("language_eval_job_id")
            if not language_eval_job_id:
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{LANGUAGE_EVAL_API_URL}/api/jobs/{language_eval_job_id}",
                        headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "jobs:read")},
                    )
                    if resp.status_code == 404:
                        log.warning(f"language-eval job {language_eval_job_id} not found — marking failed")
                        revoke_eval_tokens_for_job(job.id)
                        EvalJobs.update_job_status(
                            id=job.id,
                            update=EvalJobStatusUpdate(
                                status="failed",
                                error_message="Job not found in language-eval (may have been lost on restart)",
                            ),
                        )
                        continue
                    resp.raise_for_status()
                    remote_job = resp.json()
            except Exception as e:
                log.error(f"Failed to check language-eval job {language_eval_job_id}: {e}")
                continue

            remote_status = remote_job.get("status", "")
            if remote_status in ("completed", "failed", "cancelled"):
                error_msg = remote_job.get("error_message")
                revoke_eval_tokens_for_job(job.id)
                EvalJobs.update_job_status(
                    id=job.id,
                    update=EvalJobStatusUpdate(
                        status=remote_status,
                        error_message=error_msg,
                    ),
                )
                log.info(f"Eval job {job.id} synced to '{remote_status}' from language-eval")
        else:
            # code-eval path
            code_eval_job_id = (job.meta or {}).get("code_eval_job_id")
            if not code_eval_job_id:
                continue

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{CODE_EVAL_API_URL}/api/jobs/{code_eval_job_id}",
                        headers={TICKET_HEADER: mint_service_ticket(CODE_EVAL_AUDIENCE, "jobs:read")},
                    )
                    if resp.status_code == 404:
                        log.warning(f"code-eval job {code_eval_job_id} not found — marking failed")
                        revoke_eval_tokens_for_job(job.id)
                        EvalJobs.update_job_status(
                            id=job.id,
                            update=EvalJobStatusUpdate(
                                status="failed",
                                error_message="Job not found in code-eval (may have been lost on restart)",
                            ),
                        )
                        continue
                    resp.raise_for_status()
                    code_eval_job = resp.json()
            except Exception as e:
                log.error(f"Failed to check code-eval job {code_eval_job_id}: {e}")
                continue

            remote_status = code_eval_job.get("status", "")
            if remote_status in ("completed", "failed", "cancelled"):
                error_msg = code_eval_job.get("error_message")

                # Fetch and persist results on success
                if remote_status == "completed":
                    await _fetch_code_eval_results(job, code_eval_job_id)

                revoke_eval_tokens_for_job(job.id)
                EvalJobs.update_job_status(
                    id=job.id,
                    update=EvalJobStatusUpdate(
                        status=remote_status,
                        error_message=error_msg,
                    ),
                )
                log.info(f"Eval job {job.id} synced to '{remote_status}' from code-eval")


# Bounded backfill retries so results that code-eval genuinely lost (e.g. on
# restart) don't get re-fetched forever. Keyed by code_eval_job_id.
_reconcile_attempts: dict[str, int] = {}
_RECONCILE_MAX_ATTEMPTS = 5


async def _reconcile_missing_results() -> None:
    """Backfill results for completed code-eval jobs whose details never persisted.

    ``_fetch_code_eval_results`` runs once when a job flips to completed; if
    code-eval was busy at that moment (it serves one eval at a time) the
    fetch can fail, leaving a 'completed' job with no local results — and the
    detail view then 404s. code-eval keeps results on its own disk, so retry
    until they land. Idempotent: once the details file exists locally the job is
    skipped; capped at _RECONCILE_MAX_ATTEMPTS so permanently-lost runs stop.
    """
    try:
        completed = EvalJobs.get_jobs_by_status("completed")
    except Exception as e:
        log.warning(f"reconcile: failed to list completed jobs: {e}")
        return

    for job in completed[:50]:
        if (getattr(job, "eval_type", "code-eval") or "code-eval") != "code-eval":
            continue
        code_eval_job_id = (job.meta or {}).get("code_eval_job_id")
        if not code_eval_job_id:
            continue
        if _results_present(code_eval_job_id):
            _reconcile_attempts.pop(code_eval_job_id, None)
            continue
        if _reconcile_attempts.get(code_eval_job_id, 0) >= _RECONCILE_MAX_ATTEMPTS:
            continue
        _reconcile_attempts[code_eval_job_id] = _reconcile_attempts.get(code_eval_job_id, 0) + 1
        log.info(
            f"reconcile: backfilling results for job {job.id} "
            f"({code_eval_job_id}), attempt {_reconcile_attempts[code_eval_job_id]}"
        )
        await _fetch_code_eval_results(job, code_eval_job_id)


async def process_eval_queue() -> None:
    """Background loop: sync running job status and dispatch queued jobs.

    Polls every 10 seconds. Only one job runs at a time.
    """
    import asyncio

    while True:
        try:
            # First, sync status of any running jobs with code-eval
            await _sync_running_jobs()

            # Then, dispatch next queued job if nothing is running
            if not EvalJobs.has_running_job():
                next_job = EvalJobs.get_next_queued_job()
                if next_job:
                    eval_type = getattr(next_job, "eval_type", "code-eval") or "code-eval"
                    log.info(f"Queue processor: dispatching {eval_type} job {next_job.id}")
                    if eval_type == "language-eval":
                        await _dispatch_language_eval_job(next_job)
                    else:
                        await _dispatch_eval_job(next_job)
        except Exception as e:
            log.error(f"Eval queue processor error: {e}")
        await asyncio.sleep(10)


@router.post("/jobs/{id}/approve", response_model=Optional[EvalJobModel])
async def approve_eval_job(id: str, user=Depends(get_admin_user)):
    """Admin approves a pending eval job — places it in the queue."""
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be approved",
        )

    # Validate the user has an API key before queuing
    requesting_user = Users.get_user_by_id(job.user_id)
    if not requesting_user or not requesting_user.api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The requesting user does not have an API key configured. "
            "An API key is required to authenticate inference requests.",
        )

    return EvalJobs.update_job_status(
        id=id,
        update=EvalJobStatusUpdate(status="queued"),
    )


@router.post("/jobs/{id}/reject", response_model=Optional[EvalJobModel])
async def reject_eval_job(id: str, user=Depends(get_admin_user)):
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be rejected",
        )
    return EvalJobs.update_job_status(
        id=id,
        update=EvalJobStatusUpdate(
            status="cancelled",
            error_message="Rejected by administrator",
        ),
    )


############################
# Scheduling
############################


class ScheduleEvalJobForm(BaseModel):
    scheduled_for: int  # Unix timestamp


@router.post("/jobs/{id}/schedule", response_model=Optional[EvalJobModel])
async def schedule_eval_job(
    id: str,
    form_data: ScheduleEvalJobForm,
    user=Depends(get_admin_user),
):
    """Set or update the scheduled time for an eval job."""
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be scheduled",
        )

    import time as _time

    if form_data.scheduled_for < int(_time.time()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled time must be in the future",
        )

    return EvalJobs.update_job_scheduled_for(id=id, scheduled_for=form_data.scheduled_for)


@router.post("/jobs/{id}/unschedule", response_model=Optional[EvalJobModel])
async def unschedule_eval_job(
    id: str,
    user=Depends(get_admin_user),
):
    """Remove the schedule from an eval job, returning it to pending."""
    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status != "scheduled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only scheduled jobs can be unscheduled",
        )
    return EvalJobs.update_job_scheduled_for(id=id, scheduled_for=None)


@router.get("/jobs/{id}/events")
async def get_eval_job_events(id: str, user=Depends(get_verified_user)):
    """Return all logged eval events for a job as a JSON array.

    Reads from the same JSONL file used by the live SSE endpoint.
    Works for completed, failed, cancelled, and running jobs.
    """
    from selfai_ui.env import DATA_DIR

    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    events_file = Path(DATA_DIR) / "eval-events" / f"{id}.jsonl"

    # For dry-run language-eval jobs, events are in the language-eval container's logs dir
    is_dry_run = job.eval_type == "language-eval" and (job.meta or {}).get("dry_run", False)
    language_eval_job_id = (job.meta or {}).get("language_eval_job_id") if is_dry_run else None
    if language_eval_job_id:
        language_eval_events_file = Path(DATA_DIR) / "language-eval-logs" / f"{language_eval_job_id}.events.jsonl"
        if language_eval_events_file.exists():
            events_file = language_eval_events_file

    events = []
    if events_file.exists():
        try:
            with open(events_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception:
            pass

    # For completed language-eval jobs, enrich events with results data
    # (target, scored response, metrics) from the language-eval samples file.
    result_benchmarks = {}
    if job.eval_type == "language-eval" and job.status in ("completed", "failed"):
        try:
            all_results = await _list_language_eval_results()
            result = next((r for r in all_results if r["id"] == id), None)
            if result:
                result_benchmarks = result.get("benchmarks", {})
                disk_id = result.get("language_eval_job_id", id)
                samples = await _load_language_eval_samples(disk_id, result["model"])
                if samples and events:
                    for i, event in enumerate(events):
                        if i < len(samples):
                            sample = samples[i]
                            # Target (expected answer)
                            event["target"] = sample.get("target", "")
                            # Task name from sample
                            if "task_name" not in event or not event["task_name"]:
                                event["task_name"] = sample.get(
                                    "task_name",
                                    sample.get("doc", {}).get("task_name", ""),
                                )
                            # Scored response from filtered_resps
                            filtered = sample.get("filtered_resps", [])
                            if filtered:
                                resp = filtered[0]
                                if isinstance(resp, list) and resp:
                                    event["scored_response"] = str(resp[0])
                                else:
                                    event["scored_response"] = str(resp)
                            # Metrics — collect actual metric values
                            metric_names = sample.get("metrics", [])
                            if isinstance(metric_names, list):
                                metrics = {}
                                for m in metric_names:
                                    if m in sample:
                                        metrics[m] = sample[m]
                                if metrics:
                                    event["metrics"] = metrics
        except Exception as e:
            log.warning(f"Failed to enrich events with language-eval results: {e}")

    return {
        "events": events,
        "status": job.status,
        "benchmarks": result_benchmarks,
        "job": {
            "id": job.id,
            "eval_type": job.eval_type,
            "benchmark": job.benchmark,
            "model_id": job.model_id,
            "status": job.status,
            "meta": job.meta,
        },
    }


@router.get("/jobs/{id}/live")
async def stream_eval_job_live(id: str, user=Depends(get_verified_user)):
    """Stream live prompt/response pairs from a running eval job via SSE.

    Reads from the UI's own eval-events JSONL file, written by the chat
    completions handler when requests arrive with a JIT eval token.
    """
    import asyncio

    from selfai_ui.env import DATA_DIR

    job = EvalJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    events_file = Path(DATA_DIR) / "eval-events" / f"{id}.jsonl"

    # For dry-run language-eval jobs, proxy the SSE stream from the language-eval container
    # because dry-run events are written there, not to the UI's local events dir.
    is_dry_run = job.eval_type == "language-eval" and (job.meta or {}).get("dry_run", False)
    language_eval_job_id = (job.meta or {}).get("language_eval_job_id") if is_dry_run else None

    if language_eval_job_id:

        async def generate_proxy():
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "GET",
                        f"{LANGUAGE_EVAL_API_URL}/api/jobs/{language_eval_job_id}/live",
                        headers={TICKET_HEADER: mint_service_ticket(LANGUAGE_EVAL_AUDIENCE, "jobs:read")},
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line:
                                yield f"{line}\n"
                            else:
                                yield "\n"
            except Exception as e:
                log.warning(f"Proxy stream error for dry-run job {id}: {e}")
                yield 'event: error\ndata: {"error": "Stream proxy failed"}\n\n'

        return StreamingResponse(generate_proxy(), media_type="text/event-stream")

    async def generate():
        lines_sent = 0
        while True:
            # Read new event lines from the JSONL file
            if events_file.exists():
                try:
                    with open(events_file, "r") as f:
                        all_lines = f.readlines()
                    new_lines = all_lines[lines_sent:]
                    for line in new_lines:
                        line = line.strip()
                        if line:
                            yield f"data: {line}\n\n"
                            lines_sent += 1
                except Exception:
                    pass

            # Check if job is done
            current_job = EvalJobs.get_job_by_id(id=id)
            if current_job and current_job.status not in (
                "pending",
                "queued",
                "running",
            ):
                # Flush any remaining lines
                if events_file.exists():
                    try:
                        with open(events_file, "r") as f:
                            all_lines = f.readlines()
                        for line in all_lines[lines_sent:]:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                    except Exception:
                        pass
                yield f'event: done\ndata: {{"status": "{current_job.status}"}}\n\n'
                break

            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")

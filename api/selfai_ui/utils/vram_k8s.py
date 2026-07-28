"""Kubernetes pod-reaper capability (R5 / T-004).

The genuinely-new k8s API surface for self.ai: there is no other Kubernetes
API client anywhere in ``api/`` today (``config.py``'s ``K8S_FLAG`` is a
URL-resolution flag, not a client). This module gives R5's force-reap
escalation (T-005) a way to *delete a stale consumer's pod* when it will not
cooperate with an R2 release — the last-resort leg that reclaims VRAM without
the target's cooperation or code (R5-AC4).

Pattern (mirror of the release transport)
------------------------------------------
This deliberately mirrors :mod:`selfai_ui.utils.vram_llamolotl`'s
``LlamolotlReleaseTransport``: a :class:`PodReaper` **Protocol** + a single
concrete :class:`KubernetesPodReaper` impl, an **injectable** client so unit
tests (T-006) can drive confirmed-delete / 403 / not-found / confirm-timeout
with no real cluster, a **structured outcome** (:class:`ReapOutcome`, shaped
like ``vram_broker``'s ``ReleaseResponse``/``LeaseDenied`` house style), and a
strict **never-raise-into-the-caller** posture — every RBAC-denied, not-found,
connection, or timeout path resolves to a ``failed`` outcome, never an
exception the broker's grant loop would have to catch.

Credentials (R5-AC4)
--------------------
The concrete reaper loads **in-cluster config** — ``load_incluster_config()``
reads the pod's mounted ServiceAccount token and the cluster CA automatically.
That mounted SA is exactly "core's own service-account credentials" the kit
requires: no ticket, no target cooperation, no target-side code. This module
implements *core's side assuming the ``pods:delete``/``pods:list`` RBAC grant
exists*; provisioning that grant is the infra prerequisite the kit lists as out
of scope and is validated live only in T-007 ([BLOCKED-EXTERNAL]).

Evidentiary bar (R5-AC5, same as R2's confirmed-release)
--------------------------------------------------------
An accepted delete is NOT enough. The reaper deletes each matched pod and then
*confirms* the **specific pods it deleted** are gone (matched by uid, falling
back to name; or a 404) within ``timeout_seconds``, and only then returns
``confirmed``. Confirmation is per-pod-identity, NOT "the selector matches zero
pods": every real consumer is controller-managed (Deployment/StatefulSet), so a
reaped pod is replaced under the same selector within milliseconds — waiting for
the selector to empty would make a genuinely-successful reap look unconfirmed
forever (the failure T-007 surfaced live). A delete the API accepts but whose
target cannot be confirmed absent within the bound returns ``failed`` — the same
"confirmed or nothing" bar R2's release path uses, so T-005 only ever clears a
holder's ``held`` on the evidence of a *confirmed* deletion, never a hopeful one. An
initial list that matches no pods is likewise ``failed`` ("nothing to reap") —
there is no deletion evidence, so it must never be reported as a success that
would let the broker fabricate a held-clear.

Logging (R5-AC8)
----------------
Every attempt and outcome is logged under a distinct ``vram-reap:`` prefix so a
pod *kill* is never confused with a ``vram-broker:`` / ``vram-llamolotl:``
*cooperative* release in the logs or in the T-005 denial breakdown.
"""

import asyncio
import enum
import logging
import time
from contextlib import asynccontextmanager
from typing import (
    AsyncContextManager,
    AsyncIterator,
    Callable,
    Optional,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# How often the confirm phase re-lists pods while waiting for a delete to take
# effect. Kept small so a fast delete confirms quickly; the overall confirm wait
# is bounded by the caller's ``timeout_seconds``, never by this interval.
_CONFIRM_POLL_INTERVAL_SECONDS = 0.5


class ReapStatus(str, enum.Enum):
    """Outcome of a single force-reap against one consumer's pod(s).

    Only two states, deliberately: a reap either *confirmed* the target pod(s)
    gone (the evidentiary bar T-005 clears ``held`` on) or *failed* for a named
    reason. There is no "accepted-but-unconfirmed" success — an accepted delete
    we could not confirm gone is a ``failed`` with the reason, never a silent
    win (same posture as R2's confirmed-release, see :class:`ReapOutcome`)."""

    CONFIRMED = "confirmed"
    FAILED = "failed"


class ReapOutcome(BaseModel):
    """Structured result of a force-reap (house style: a small pydantic model,
    like ``vram_broker``'s ``ReleaseResponse`` / ``LeaseDenied``).

    ``status`` is the machine-readable verdict; ``reason`` always carries a
    human-readable explanation (why it failed, or what was confirmed) so T-005
    can surface it in an R3 ``LeaseDenied`` breakdown (R5-AC6); ``pods_deleted``
    is how many matched pods the reaper issued a delete for (0 on a
    nothing-to-reap or list-error failure)."""

    status: ReapStatus
    reason: str
    pods_deleted: int = 0

    @classmethod
    def confirmed(cls, pods_deleted: int, reason: str = "") -> "ReapOutcome":
        return cls(status=ReapStatus.CONFIRMED, reason=reason, pods_deleted=pods_deleted)

    @classmethod
    def failed(cls, reason: str, pods_deleted: int = 0) -> "ReapOutcome":
        return cls(status=ReapStatus.FAILED, reason=reason, pods_deleted=pods_deleted)


@runtime_checkable
class PodReaper(Protocol):
    """The force-reap capability the broker's escalation (T-005) calls.

    Amount-free and mechanism-free from the broker's side: it names a target
    (``namespace`` + label ``selector``) and a time bound, and gets back a
    structured :class:`ReapOutcome`. Whether the deletion goes via the k8s API,
    a Deployment scale-to-zero, or anything else is entirely this layer's
    concern. Never raises — a failure is a ``failed`` :class:`ReapOutcome`."""

    async def reap(
        self, namespace: str, selector: str, timeout_seconds: float
    ) -> ReapOutcome:
        ...


class PodApi(Protocol):
    """The minimal slice of the k8s ``CoreV1Api`` the reaper needs, expressed as
    a Protocol so a unit test can hand in a fake with no cluster (T-006).

    Signatures mirror ``kubernetes_asyncio.client.CoreV1Api``: ``label_selector``
    is the standard kwarg, ``list_namespaced_pod`` returns a ``V1PodList`` whose
    ``.items[*].metadata.name`` we read, and ``delete_namespaced_pod`` takes the
    pod name then the namespace."""

    async def list_namespaced_pod(self, namespace: str, label_selector: str):
        ...

    async def delete_namespaced_pod(self, name: str, namespace: str):
        ...


# A factory yielding a ready-to-use :class:`PodApi` inside an async context
# manager (so the concrete impl can open and reliably close the underlying
# ``ApiClient`` session). Injectable: tests pass a factory yielding a fake.
ClientFactory = Callable[[], AsyncContextManager[PodApi]]


@asynccontextmanager
async def _incluster_core_v1() -> AsyncIterator[PodApi]:
    """Default :data:`ClientFactory`: an in-cluster ``CoreV1Api`` session.

    ``kubernetes_asyncio`` is imported *lazily here* — the module must import
    (and unit tests with an injected fake factory must run) on hosts where the
    client library is not installed; only a real reap against a real cluster
    touches the dependency. ``load_incluster_config()`` reads the pod's mounted
    ServiceAccount token + cluster CA (R5-AC4's "core's own credentials"); it is
    synchronous in ``kubernetes_asyncio`` (it only reads local files/env)."""
    from kubernetes_asyncio import client, config

    config.load_incluster_config()
    api_client = client.ApiClient()
    try:
        yield client.CoreV1Api(api_client)
    finally:
        # Always close the aiohttp session the ApiClient owns, even on error.
        await api_client.close()


def _status_code(exc: Exception) -> Optional[int]:
    """The HTTP status carried by a ``kubernetes_asyncio`` ``ApiException``
    (``.status``), duck-typed so tests can raise any exception with a ``.status``
    and so we never have to import the client library just to catch its error
    type. Returns ``None`` for connection/transport errors with no status."""
    code = getattr(exc, "status", None)
    if isinstance(code, bool):  # bool is an int subclass — never a real status
        return None
    if isinstance(code, int):
        return code
    try:
        return int(code)  # some clients stash the code as a numeric string
    except (TypeError, ValueError):
        return None


def _pod_names(pod_list) -> list[str]:
    """Extract pod names from a ``V1PodList``-shaped object, defensively (so a
    test fake need only expose ``.items[*].metadata.name``)."""
    return [name for name, _ in _pod_targets(pod_list)]


def _pod_targets(pod_list) -> list[tuple[str, Optional[str]]]:
    """Extract ``(name, uid)`` for each pod in a ``V1PodList``-shaped object.

    The ``uid`` is the *stable identity* the confirm phase verifies a specific
    pod's deletion by. A Deployment/StatefulSet consumer's controller replaces a
    reaped pod under the SAME label selector within milliseconds, so "no pod
    matches the selector" never becomes true for a managed consumer — the reap we
    just performed would look unconfirmed forever (found live in T-007). "The pod
    we deleted (this uid) is gone" is the property that actually holds, and it is
    what frees the VRAM: the replacement comes up with no model loaded. ``uid``
    also disambiguates a StatefulSet's same-*name* recreation (new uid). Defensive
    so a test fake need only expose ``.items[*].metadata.name`` (uid optional)."""
    items = getattr(pod_list, "items", None) or []
    targets: list[tuple[str, Optional[str]]] = []
    for item in items:
        meta = getattr(item, "metadata", None)
        name = getattr(meta, "name", None) if meta is not None else None
        uid = getattr(meta, "uid", None) if meta is not None else None
        if isinstance(name, str) and name:
            targets.append((name, uid if isinstance(uid, str) and uid else None))
    return targets


class KubernetesPodReaper:
    """Concrete :class:`PodReaper` backed by the Kubernetes API.

    Lists the pods matching ``selector`` in ``namespace``, issues a delete for
    each, then confirms they are gone within ``timeout_seconds`` before ever
    reporting ``confirmed``. Every failure mode — RBAC-denied (403), not-found
    (404), connection error, or confirm-timeout — resolves to a ``failed``
    :class:`ReapOutcome` with the reason and NEVER raises into the caller (the
    broker's grant loop must be able to fall through to a structured denial,
    R5-AC6). The k8s client is injected via ``client_factory`` so tests drive
    every path with no cluster."""

    def __init__(
        self,
        client_factory: Optional[ClientFactory] = None,
        poll_interval_seconds: float = _CONFIRM_POLL_INTERVAL_SECONDS,
    ):
        self._client_factory: ClientFactory = client_factory or _incluster_core_v1
        self._poll_interval = max(0.0, float(poll_interval_seconds))

    async def reap(
        self, namespace: str, selector: str, timeout_seconds: float
    ) -> ReapOutcome:
        log.info(
            "vram-reap: attempting force-reap of pods selector=%r in namespace=%r "
            "(confirm within %.1fs) using core's mounted ServiceAccount",
            selector,
            namespace,
            timeout_seconds,
        )
        try:
            async with self._client_factory() as api:
                # --- list the target pods -------------------------------------
                try:
                    pod_list = await api.list_namespaced_pod(
                        namespace, label_selector=selector
                    )
                except Exception as e:
                    return self._failed_from_exc("list", namespace, selector, e)

                targets = _pod_targets(pod_list)
                if not targets:
                    # Nothing matched. There is no deletion to confirm and no
                    # evidence any VRAM was freed, so this is a FAILURE, not a
                    # no-op success — reporting confirmed here would let T-005
                    # clear a holder's held with no evidence (trust invariant).
                    reason = (
                        f"no pods matched selector {selector!r} in namespace "
                        f"{namespace!r} — nothing to reap"
                    )
                    log.warning("vram-reap: %s", reason)
                    return ReapOutcome.failed(reason)
                names = [name for name, _ in targets]

                # --- delete each matched pod ----------------------------------
                deleted = 0
                for name in names:
                    try:
                        await api.delete_namespaced_pod(name, namespace)
                        deleted += 1
                        log.info(
                            "vram-reap: issued delete for pod %r in namespace %r",
                            name,
                            namespace,
                        )
                    except Exception as e:
                        code = _status_code(e)
                        if code == 404:
                            # Already gone between list and delete — that is
                            # progress toward "absent", not a failure. Count it
                            # and keep going; the confirm phase is the arbiter.
                            deleted += 1
                            log.info(
                                "vram-reap: pod %r already absent (404) on delete "
                                "in namespace %r",
                                name,
                                namespace,
                            )
                            continue
                        return self._failed_from_exc(
                            "delete", namespace, selector, e, pods_deleted=deleted
                        )

                # --- confirm the pods are actually gone (R5-AC5) --------------
                if await self._confirm_absent(
                    api, namespace, selector, targets, timeout_seconds
                ):
                    reason = (
                        f"confirmed {deleted} pod(s) deleted and absent for selector "
                        f"{selector!r} in namespace {namespace!r}"
                    )
                    log.info("vram-reap: %s", reason)
                    return ReapOutcome.confirmed(pods_deleted=deleted, reason=reason)

                # Deletes accepted but pods not confirmed gone within the bound.
                # Same bar as R2: an unconfirmed release is not a release.
                reason = (
                    f"delete accepted but pods for selector {selector!r} in namespace "
                    f"{namespace!r} still present after {timeout_seconds:.1f}s — "
                    "not confirmed gone"
                )
                log.warning("vram-reap: %s", reason)
                return ReapOutcome.failed(reason, pods_deleted=deleted)
        except Exception as e:
            # Belt-and-braces: any unexpected error (config load, session setup,
            # a client raising something without a status) is a failed reap, not
            # an exception the broker's grant loop has to catch.
            reason = (
                f"unexpected error during force-reap of selector {selector!r} in "
                f"namespace {namespace!r}: {e!r}"
            )
            log.warning("vram-reap: %s", reason)
            return ReapOutcome.failed(reason)

    async def _confirm_absent(
        self,
        api: PodApi,
        namespace: str,
        selector: str,
        targets: list[tuple[str, Optional[str]]],
        timeout_seconds: float,
    ) -> bool:
        """Poll ``list_namespaced_pod`` until the *specific pods we deleted* are
        gone (or a 404 says the resource is gone) within ``timeout_seconds``.

        Confirmation is per-pod-identity, NOT "the selector matches zero pods":
        every real consumer here is controller-managed (Deployment/StatefulSet),
        so a reaped pod is replaced under the same selector within milliseconds
        and the selector never empties — the reap that genuinely freed the VRAM
        would otherwise look unconfirmed forever (the T-007 failure mode). A
        target is matched by ``uid`` when known (so a StatefulSet's same-*name*
        recreation, new uid, still confirms), falling back to ``name`` when the
        cluster/list gave no uid. The replacement pod's own (new) identity is
        deliberately ignored. Returns ``True`` only on confirmed-absent, ``False``
        on timeout. Checks at least once even when ``timeout_seconds`` is 0."""
        target_uids = {uid for _, uid in targets if uid}
        target_names_without_uid = {name for name, uid in targets if not uid}
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                pod_list = await api.list_namespaced_pod(
                    namespace, label_selector=selector
                )
                current = _pod_targets(pod_list)
                current_uids = {uid for _, uid in current if uid}
                current_names = {name for name, _ in current}
                still_present = (target_uids & current_uids) or (
                    target_names_without_uid & current_names
                )
                if not still_present:
                    # Every pod we deleted is gone; a differently-identified
                    # replacement (the controller's fresh, model-free pod) does
                    # not count against confirmation.
                    return True
            except Exception as e:
                if _status_code(e) == 404:
                    # The pods (or their namespace) are gone entirely — absent.
                    return True
                # A transient error mid-confirm (including a 403 that will not
                # resolve): keep polling until the deadline, then report failed.
                log.warning(
                    "vram-reap: confirm poll error for selector %r in namespace %r "
                    "(%r); retrying until timeout",
                    selector,
                    namespace,
                    e,
                )
            if time.monotonic() >= deadline:
                return False
            # Never sleep past the deadline.
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(self._poll_interval, max(0.0, remaining)))

    def _failed_from_exc(
        self,
        phase: str,
        namespace: str,
        selector: str,
        exc: Exception,
        pods_deleted: int = 0,
    ) -> ReapOutcome:
        """Turn a k8s client exception into a ``failed`` :class:`ReapOutcome`
        with a phase- and status-specific reason (RBAC-denied / not-found /
        other API error / connection error). Never re-raises."""
        code = _status_code(exc)
        if code == 403:
            reason = (
                f"RBAC denied ({phase}, HTTP 403) for selector {selector!r} in "
                f"namespace {namespace!r}: {exc!r}"
            )
        elif code == 404:
            reason = (
                f"not found ({phase}, HTTP 404) for selector {selector!r} in "
                f"namespace {namespace!r}: {exc!r}"
            )
        elif code is not None:
            reason = (
                f"k8s API error ({phase}, HTTP {code}) for selector {selector!r} in "
                f"namespace {namespace!r}: {exc!r}"
            )
        else:
            reason = (
                f"connection/transport error ({phase}) for selector {selector!r} in "
                f"namespace {namespace!r}: {exc!r}"
            )
        log.warning("vram-reap: %s", reason)
        return ReapOutcome.failed(reason, pods_deleted=pods_deleted)

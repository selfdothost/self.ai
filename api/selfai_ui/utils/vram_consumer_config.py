"""Core's trusted, config-derived view of who the VRAM lease consumers are
(self.ai#79).

WHY THIS EXISTS
---------------
``POST /api/v1/vram-leases/register`` is gated on a service ticket, and a service
ticket proves the caller holds the mesh's ONE shared ``SERVICE_AUTH_SECRET`` — it
does not say WHICH service is calling (``aud`` names the callee, ``iss`` is
deliberately unpinned). Every service in the mesh can therefore register under
any ``consumer_id``, and the registration form carries fields that are not
self-reports at all but POLICY:

* ``priority`` decides who may reclaim VRAM from whom. A consumer that registers
  itself at priority 99 outranks the inference brain and can evict it — the
  priority inversion the ``h.priority < priority`` gate exists to prevent.
* ``total_capacity_bytes`` feeds ``total_capacity()``, which is a ``max()`` over
  rows, so ONE row inflates the whole pool ceiling and with it ``free_capacity()``
  — i.e. invents VRAM.
* ``k8s_namespace``/``k8s_pod_selector`` aim force-reap. (Already defused
  separately in self.ai#75: the broker now takes reap targets from
  ``set_reap_targets`` rather than the row.)

None of those are things a consumer should get to assert about itself. They are
operator configuration, and core already has them — the same env this module
reads is what core registers each consumer with at startup.

So this is the trusted answer, and the HTTP boundary defers to it: a registration
for a configured consumer keeps its ``held_bytes`` self-report (that IS a
self-report) and takes everything else from here.

Read at call time, not import time, so tests and a restarted process both see
current env.
"""

import logging
from typing import Optional

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


# The config-driven consumers, and the env each one's policy comes from.
# Mirrors main.py's `_register_*_vram_consumer` hooks — same values, one source.
_CONSUMER_ENV = (
    ("self.llamolotl", "LLAMOLOTL_VRAM_CAPACITY_BYTES", "LLAMOLOTL_VRAM_LEASE_PRIORITY",
     "LLAMOLOTL_K8S_NAMESPACE", "LLAMOLOTL_K8S_POD_SELECTOR"),
    ("self.speak", "SPEAK_VRAM_CAPACITY_BYTES", "SPEAK_VRAM_LEASE_PRIORITY", None, None),
    ("self.sketch", "SKETCH_VRAM_CAPACITY_BYTES", "SKETCH_VRAM_LEASE_PRIORITY", None, None),
    ("self.curator", "CURATOR_VRAM_CAPACITY_BYTES", "CURATOR_VRAM_LEASE_PRIORITY",
     "CURATOR_K8S_NAMESPACE", "CURATOR_K8S_POD_SELECTOR"),
    # self.publish (self.ai#136): no namespace/selector — the merge runs inside
    # llamolotl's pod, so force-reaping "the publish" would mean killing
    # llamolotl, which is llamolotl's own row's business.
    ("self.publish", "PUBLISH_VRAM_CAPACITY_BYTES", "PUBLISH_VRAM_LEASE_PRIORITY", None, None),
)


def _env_value(name: Optional[str]):
    if not name:
        return None
    from selfai_ui import env as env_module

    return getattr(env_module, name, None)


def _as_int(value) -> Optional[int]:
    """``None`` for unset/blank/malformed — never a fabricated number."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def trusted_consumer_config() -> dict:
    """``{consumer_id: {capacity, priority, namespace, selector}}`` for every
    consumer core is configured for.

    A consumer whose capacity env is unset/malformed is ABSENT: core has no
    policy for it, so there is nothing to enforce and nothing to trust. Entries
    only ever carry values core itself owns.
    """
    config = {}
    for consumer_id, cap_env, prio_env, ns_env, sel_env in _CONSUMER_ENV:
        capacity = _as_int(_env_value(cap_env))
        if capacity is None:
            continue
        namespace = (_env_value(ns_env) or "").strip() or None
        selector = (_env_value(sel_env) or "").strip() or None
        config[consumer_id] = {
            "capacity": capacity,
            "priority": _as_int(_env_value(prio_env)) or 0,
            "namespace": namespace,
            "selector": selector,
        }
    return config


def apply_trusted_policy(form):
    """Return ``form`` with its policy fields replaced by core's configuration,
    or ``None`` if core has no configuration for that ``consumer_id``.

    ``held_bytes`` is deliberately left as sent — how much VRAM a consumer is
    holding is exactly the kind of thing only the consumer can know, and the
    trust invariant already treats it as a self-report. Everything else is
    policy and comes from core.

    ``None`` (unknown consumer) is a refusal rather than a silent pass-through:
    core tracks the consumers it is configured for, and letting an arbitrary
    ``consumer_id`` insert a row would hand back the capacity-inflation hole this
    module closes — a fresh row with a huge ``total_capacity_bytes`` raises
    ``total_capacity()`` for everyone regardless of what the known consumers say.
    """
    config = trusted_consumer_config().get(form.consumer_id)
    if config is None:
        return None
    return form.model_copy(
        update={
            "total_capacity_bytes": config["capacity"],
            "priority": config["priority"],
            "k8s_namespace": config["namespace"],
            "k8s_pod_selector": config["selector"],
        }
    )

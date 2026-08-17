"""The mechanical half of a publish — self.ai#131 R6.

Separated from `utils/model_versions.py` so the record semantics (what a
publish means) do not import the router that talks to llamolotl (how a publish
is performed). `routers/llamolotl.py` already imports the versioning service;
putting this call there too would close the cycle.

The merge itself is llamolotl's existing bake pipeline: `merge-lora` into the
base, convert, and optionally quantize. That is the same endpoint the Tier 2
work deliberately did NOT wire to the cheap tier — a bake in this sense
produces a base GGUF, which is exactly what a publish is and exactly what
`R5`'s adapter commit forbids.
"""

import json
import logging

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


async def merge_adapters_into_base(
    app_state,
    base,
    adapters,
    output_name: str,
    quant_type=None,
    url_idx: int = 0,
) -> dict:
    """Ask llamolotl to merge `adapters` into `base` and emit `output_name`.

    Raises on any transport failure so `publish_line` records the job as failed
    and writes no version — a publish that quietly returned nothing would leave
    a line claiming a base GGUF that was never produced.
    """
    from selfai_ui.routers.llamolotl import LLAMOLOTL_AUDIENCE, get_api_key, send_post_request
    from selfai_ui.utils.service_auth import mint_service_ticket

    control_urls = app_state.config.LLAMOLOTL_CONTROL_BASE_URLS or []
    if url_idx >= len(control_urls):
        raise RuntimeError(f"no llamolotl control URL at index {url_idx}")

    base_urls = app_state.config.LLAMOLOTL_BASE_URLS or []
    key = get_api_key(base_urls[url_idx], app_state.config.LLAMOLOTL_API_CONFIGS) if base_urls else None

    payload = {
        "base_model": base.artifact_ref,
        # Scale is not carried on the version record: an adapter version says
        # what was fitted, not how strongly it was being previewed at the time.
        # A publish merges each at full weight; a partial merge would produce a
        # base nothing on the line describes.
        "adapters": [{"path": adapter.artifact_ref, "weight": 1.0} for adapter in adapters],
        "output_name": output_name,
    }
    if quant_type:
        payload["quant_type"] = quant_type

    result = await send_post_request(
        url=f"{control_urls[url_idx]}/api/pipeline/bake",
        payload=json.dumps(payload),
        stream=False,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "pipeline:write"),
    )
    if result is None:
        raise RuntimeError("llamolotl did not answer the merge request")
    return result

import asyncio
import json
import logging
import random
import urllib.parse
import urllib.request
from typing import Optional

import websocket  # NOTE: websocket-client (https://github.com/websocket-client/websocket-client)
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["COMFYUI"])

default_headers = {"User-Agent": "Mozilla/5.0"}

# Per-recv ceiling on the progress WebSocket. ComfyUI streams frequent messages
# while sampling; the only long gap is a cold checkpoint load, so this is
# generous. Without it a dropped connection (or, before the execution_error
# handling below, a failed prompt) would block get_images forever.
WS_RECV_TIMEOUT = 300


def queue_prompt(prompt, client_id, base_url, api_key):
    log.info("queue_prompt")
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode("utf-8")
    log.debug(f"queue_prompt data: {data}")
    try:
        req = urllib.request.Request(
            f"{base_url}/prompt",
            data=data,
            headers={**default_headers, "Authorization": f"Bearer {api_key}"},
        )
        response = urllib.request.urlopen(req).read()
        return json.loads(response)
    except Exception as e:
        log.exception(f"Error while queuing prompt: {e}")
        raise e


def get_image(filename, subfolder, folder_type, base_url, api_key):
    log.info("get_image")
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    req = urllib.request.Request(
        f"{base_url}/view?{url_values}",
        headers={**default_headers, "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req) as response:
        return response.read()


def get_image_url(filename, subfolder, folder_type, base_url):
    log.info("get_image")
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    return f"{base_url}/view?{url_values}"


def get_history(prompt_id, base_url, api_key):
    log.info("get_history")

    req = urllib.request.Request(
        f"{base_url}/history/{prompt_id}",
        headers={**default_headers, "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def get_images(ws, prompt, client_id, base_url, api_key):
    prompt_id = queue_prompt(prompt, client_id, base_url, api_key)["prompt_id"]
    output_images = []
    ws.settimeout(WS_RECV_TIMEOUT)
    while True:
        try:
            out = ws.recv()
        except websocket.WebSocketTimeoutException:
            log.error(f"ComfyUI: no progress within {WS_RECV_TIMEOUT}s for prompt {prompt_id}")
            raise
        if not isinstance(out, str):
            continue  # previews are binary data
        message = json.loads(out)
        mtype = message.get("type")
        data = message.get("data", {})
        # Only act on our own prompt (client_id is per-request, but be strict).
        if mtype == "executing" and data.get("node") is None and data.get("prompt_id") == prompt_id:
            break  # Execution is done
        elif mtype == "execution_error" and data.get("prompt_id") == prompt_id:
            # Upstream ComfyUI emits this on a failed graph — without handling it
            # the loop would spin until WS_RECV_TIMEOUT instead of surfacing why.
            msg = data.get("exception_message") or data.get("exception_type") or data
            log.error(f"ComfyUI execution_error for prompt {prompt_id}: {data}")
            raise RuntimeError(f"ComfyUI execution error: {msg}")
        elif mtype == "execution_interrupted" and data.get("prompt_id") == prompt_id:
            log.error(f"ComfyUI execution_interrupted for prompt {prompt_id}: {data}")
            raise RuntimeError(f"ComfyUI execution interrupted for prompt {prompt_id}")

    history = get_history(prompt_id, base_url, api_key)[prompt_id]
    for node_id in history["outputs"]:
        node_output = history["outputs"][node_id]
        if "images" in node_output:
            for image in node_output["images"]:
                url = get_image_url(image["filename"], image["subfolder"], image["type"], base_url)
                output_images.append({"url": url})
    return {"data": output_images}


class ComfyUINodeInput(BaseModel):
    type: Optional[str] = None
    node_ids: list[str] = []
    key: Optional[str] = "text"
    value: Optional[str] = None


class ComfyUIWorkflow(BaseModel):
    workflow: str
    nodes: list[ComfyUINodeInput]


class ComfyUIGenerateImageForm(BaseModel):
    workflow: ComfyUIWorkflow

    prompt: str
    negative_prompt: Optional[str] = None
    width: int
    height: int
    n: int = 1

    steps: Optional[int] = None
    seed: Optional[int] = None


async def comfyui_generate_image(model: str, payload: ComfyUIGenerateImageForm, client_id, base_url, api_key):
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    workflow = json.loads(payload.workflow.workflow)

    for node in payload.workflow.nodes:
        if node.type:
            if node.type == "model":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key] = model
            elif node.type == "prompt":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "text"] = payload.prompt
            elif node.type == "negative_prompt":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "text"] = payload.negative_prompt
            elif node.type == "width":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "width"] = payload.width
            elif node.type == "height":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "height"] = payload.height
            elif node.type == "n":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "batch_size"] = payload.n
            elif node.type == "steps":
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key if node.key else "steps"] = payload.steps
            elif node.type == "seed":
                seed = payload.seed if payload.seed else random.randint(0, 18446744073709551614)
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key] = seed
        else:
            for node_id in node.node_ids:
                workflow[node_id]["inputs"][node.key] = node.value

    try:
        ws = websocket.WebSocket()
        headers = {"Authorization": f"Bearer {api_key}"}
        ws.connect(f"{ws_url}/ws?clientId={client_id}", header=headers)
        log.info("WebSocket connection established.")
    except Exception as e:
        log.exception(f"Failed to connect to WebSocket server: {e}")
        return None

    try:
        log.info("Sending workflow to WebSocket server.")
        log.info(f"Workflow: {workflow}")
        images = await asyncio.to_thread(get_images, ws, workflow, client_id, base_url, api_key)
    except Exception as e:
        log.exception(f"Error while receiving images: {e}")
        images = None

    ws.close()

    return images

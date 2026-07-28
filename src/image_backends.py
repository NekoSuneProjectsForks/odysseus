"""
image_backends.py

Self-hosted image generation backends, as an alternative to the
OpenAI-compatible path already in mcp_servers/image_gen_server.py. Each
adapter takes a prompt + settings and returns raw PNG bytes; the caller
(image_gen_server.py) handles saving to disk/gallery, which stays identical
across all backends.

Automatic1111 (SD WebUI) and SD.Next expose the same `/sdapi/v1/txt2img`
contract, so both share one adapter. ComfyUI is graph-based: a minimal
default txt2img workflow is submitted, polled via its history endpoint, and
the resulting image fetched via /view.
"""

import asyncio
import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_GEN_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0)


class ImageBackendError(Exception):
    """Raised for user-facing errors (backend unreachable, bad response, timeout)."""


async def generate_via_a1111_compatible(
    prompt: str,
    base_url: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
    cfg_scale: float = 7.0,
    checkpoint: str = "",
    lora: str = "",
    init_image_b64: Optional[str] = None,
    denoising_strength: float = 0.75,
) -> bytes:
    """Automatic1111 / SD WebUI / SD.Next — all speak the same txt2img/img2img API.

    Passing `init_image_b64` (a bare base64 PNG, no `data:` prefix) switches to
    img2img; otherwise this is a plain txt2img call. `lora` is appended to the
    prompt as an `<lora:name:1>` tag — that's how A1111-family APIs apply LoRAs,
    there's no separate payload field for it.
    """
    if not base_url:
        raise ImageBackendError("No server URL configured for this backend")
    effective_prompt = f"{prompt} <lora:{lora}:1>" if lora else prompt
    payload = {
        "prompt": effective_prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
    }
    if checkpoint:
        payload["override_settings"] = {"sd_model_checkpoint": checkpoint}
        payload["override_settings_restore_afterwards"] = True
    if init_image_b64:
        endpoint = "/sdapi/v1/img2img"
        payload["init_images"] = [init_image_b64]
        payload["denoising_strength"] = denoising_strength
    else:
        endpoint = "/sdapi/v1/txt2img"
    url = base_url.rstrip("/") + endpoint

    try:
        async with httpx.AsyncClient(timeout=_GEN_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise ImageBackendError(f"Timed out contacting {url}") from exc
    except httpx.RequestError as exc:
        raise ImageBackendError(f"Could not reach {url}: {exc}") from exc

    if resp.status_code != 200:
        raise ImageBackendError(f"{url} returned {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    images = data.get("images") or []
    if not images:
        raise ImageBackendError("No images returned")
    return base64.b64decode(images[0])


async def list_a1111_checkpoints(base_url: str) -> list[dict]:
    """GET /sdapi/v1/sd-models — available checkpoints for A1111/SD.Next."""
    if not base_url:
        raise ImageBackendError("No server URL configured for this backend")
    url = base_url.rstrip("/") + "/sdapi/v1/sd-models"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise ImageBackendError(f"Could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise ImageBackendError(f"{url} returned {resp.status_code}")
    return [
        {"title": m.get("title", ""), "model_name": m.get("model_name", "")}
        for m in (resp.json() or [])
    ]


async def list_a1111_loras(base_url: str) -> list[str]:
    """GET /sdapi/v1/loras — available LoRA names for A1111/SD.Next."""
    if not base_url:
        raise ImageBackendError("No server URL configured for this backend")
    url = base_url.rstrip("/") + "/sdapi/v1/loras"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise ImageBackendError(f"Could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise ImageBackendError(f"{url} returned {resp.status_code}")
    return [m.get("name", "") for m in (resp.json() or []) if m.get("name")]


_COMFYUI_POLL_INTERVAL = 1.0
_COMFYUI_MAX_POLLS = 180  # ~3 minutes


def _comfyui_default_workflow(
    prompt: str, checkpoint: str, width: int, height: int, seed: int,
    lora: str = "", negative_prompt: str = "",
) -> dict:
    """A minimal standard txt2img graph: checkpoint -> (optional LoRA) -> CLIP
    encode (pos/neg) -> empty latent -> KSampler -> VAE decode -> SaveImage.
    Works with any checkpoint that has bundled CLIP + VAE (i.e. not requiring
    separate loaders), which covers the common SD1.5/SDXL single-file checkpoints."""
    model_link = ["4", 0]
    clip_link = ["4", 1]
    graph = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
    }
    if lora:
        graph["10"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": model_link, "clip": clip_link, "lora_name": lora,
                "strength_model": 1.0, "strength_clip": 1.0,
            },
        }
        model_link = ["10", 0]
        clip_link = ["10", 1]
    graph.update({
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed, "steps": 20, "cfg": 7.0, "sampler_name": "euler",
                "scheduler": "normal", "denoise": 1.0,
                "model": model_link, "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
            },
        },
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": clip_link}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": clip_link}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "odysseus", "images": ["8", 0]}},
    })
    return graph


def _comfyui_img2img_workflow(
    prompt: str, checkpoint: str, seed: int, uploaded_filename: str,
    denoise: float, lora: str = "", negative_prompt: str = "",
) -> dict:
    """Same graph as txt2img but latent comes from VAEEncode(LoadImage) instead
    of EmptyLatentImage, and KSampler.denoise < 1.0 preserves the source image."""
    graph = _comfyui_default_workflow(prompt, checkpoint, 1024, 1024, seed, lora=lora, negative_prompt=negative_prompt)
    graph["11"] = {"class_type": "LoadImage", "inputs": {"image": uploaded_filename}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["4", 2]}}
    graph["3"]["inputs"]["denoise"] = denoise
    return graph


async def _comfyui_upload_image(base: str, client: httpx.AsyncClient, image_bytes: bytes) -> str:
    files = {"image": ("init.png", image_bytes, "image/png")}
    resp = await client.post(f"{base}/upload/image", files=files)
    if resp.status_code != 200:
        raise ImageBackendError(f"ComfyUI image upload failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    name = data.get("name")
    if not name:
        raise ImageBackendError("ComfyUI did not return an uploaded filename")
    return name


async def generate_via_comfyui(
    prompt: str,
    base_url: str,
    checkpoint: str,
    width: int = 1024,
    height: int = 1024,
    negative_prompt: str = "",
    lora: str = "",
    init_image_bytes: Optional[bytes] = None,
    denoising_strength: float = 0.75,
) -> bytes:
    if not base_url:
        raise ImageBackendError("No server URL configured for ComfyUI")
    if not checkpoint:
        raise ImageBackendError(
            "No checkpoint configured for ComfyUI — set one in Settings > Image Generation "
            "(must match a filename ComfyUI's CheckpointLoaderSimple can see)"
        )
    base = base_url.rstrip("/")
    import random
    seed = random.randint(0, 2**31 - 1)

    try:
        async with httpx.AsyncClient(timeout=_GEN_TIMEOUT) as client:
            if init_image_bytes:
                uploaded_name = await _comfyui_upload_image(base, client, init_image_bytes)
                workflow = _comfyui_img2img_workflow(
                    prompt, checkpoint, seed, uploaded_name, denoising_strength,
                    lora=lora, negative_prompt=negative_prompt,
                )
            else:
                workflow = _comfyui_default_workflow(
                    prompt, checkpoint, width, height, seed, lora=lora, negative_prompt=negative_prompt,
                )
            submit = await client.post(f"{base}/prompt", json={"prompt": workflow})
            if submit.status_code != 200:
                raise ImageBackendError(f"ComfyUI rejected the workflow ({submit.status_code}): {submit.text[:300]}")
            prompt_id = submit.json().get("prompt_id")
            if not prompt_id:
                raise ImageBackendError("ComfyUI did not return a prompt_id")

            for _ in range(_COMFYUI_MAX_POLLS):
                await asyncio.sleep(_COMFYUI_POLL_INTERVAL)
                hist_resp = await client.get(f"{base}/history/{prompt_id}")
                if hist_resp.status_code != 200:
                    continue
                hist = hist_resp.json().get(prompt_id)
                if not hist:
                    continue
                outputs = hist.get("outputs", {})
                image_ref = None
                for node_output in outputs.values():
                    for img in node_output.get("images", []):
                        image_ref = img
                        break
                    if image_ref:
                        break
                if image_ref:
                    view_params = {
                        "filename": image_ref["filename"],
                        "subfolder": image_ref.get("subfolder", ""),
                        "type": image_ref.get("type", "output"),
                    }
                    img_resp = await client.get(f"{base}/view", params=view_params)
                    if img_resp.status_code != 200:
                        raise ImageBackendError(f"Could not fetch generated image ({img_resp.status_code})")
                    return img_resp.content
            raise ImageBackendError("Timed out waiting for ComfyUI to finish generating")
    except httpx.TimeoutException as exc:
        raise ImageBackendError(f"Timed out contacting {base}") from exc
    except httpx.RequestError as exc:
        raise ImageBackendError(f"Could not reach {base}: {exc}") from exc


async def _comfyui_object_info_options(base_url: str, node_class: str, field: str) -> list[str]:
    """GET /object_info/{node_class} — ComfyUI's introspection endpoint. The
    field's valid options are nested as input.required.{field}[0] (a list)."""
    if not base_url:
        raise ImageBackendError("No server URL configured for ComfyUI")
    base = base_url.rstrip("/")
    url = f"{base}/object_info/{node_class}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise ImageBackendError(f"Could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise ImageBackendError(f"{url} returned {resp.status_code}")
    data = resp.json()
    node_info = data.get(node_class) or {}
    try:
        options = node_info["input"]["required"][field][0]
    except (KeyError, IndexError, TypeError):
        return []
    return [str(o) for o in options] if isinstance(options, list) else []


async def list_comfyui_checkpoints(base_url: str) -> list[str]:
    return await _comfyui_object_info_options(base_url, "CheckpointLoaderSimple", "ckpt_name")


async def list_comfyui_loras(base_url: str) -> list[str]:
    return await _comfyui_object_info_options(base_url, "LoraLoader", "lora_name")


async def generate_image_self_hosted(
    backend: str, prompt: str, settings: dict,
    negative_prompt: str = "", init_image_bytes: Optional[bytes] = None,
    denoising_strength: float = 0.75,
) -> bytes:
    """Dispatch to the configured self-hosted backend. `backend` is one of
    'automatic1111', 'sdnext', 'comfyui' (validated by the caller). Model/LoRA
    overrides are read from settings (Settings > Image Generation)."""
    if backend in ("automatic1111", "sdnext"):
        prefix = "a1111" if backend == "automatic1111" else "sdnext"
        init_b64 = base64.b64encode(init_image_bytes).decode("ascii") if init_image_bytes else None
        return await generate_via_a1111_compatible(
            prompt,
            settings.get(f"image_backend_{prefix}_url", ""),
            negative_prompt=negative_prompt,
            checkpoint=settings.get(f"image_backend_{prefix}_model", ""),
            lora=settings.get(f"image_backend_{prefix}_lora", ""),
            init_image_b64=init_b64,
            denoising_strength=denoising_strength,
        )
    if backend == "comfyui":
        return await generate_via_comfyui(
            prompt,
            settings.get("image_backend_comfyui_url", ""),
            settings.get("image_backend_comfyui_checkpoint", ""),
            negative_prompt=negative_prompt,
            lora=settings.get("image_backend_comfyui_lora", ""),
            init_image_bytes=init_image_bytes,
            denoising_strength=denoising_strength,
        )
    raise ImageBackendError(f"Unknown self-hosted image backend: {backend}")

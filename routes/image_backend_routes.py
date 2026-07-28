"""Self-hosted image backend discovery routes — /api/image-backends/*.

Proxies "list available checkpoints/LoRAs" calls to the admin-configured
Automatic1111/SD.Next/ComfyUI server. Proxied server-side (not called
directly from the browser) so the admin's internal image-gen server doesn't
need CORS configured and LAN/localhost URLs work regardless of mixed-content
browser restrictions. Admin-only, same as every other settings-affecting route.
"""

import logging

from fastapi import APIRouter, Request, HTTPException, Query

from core.middleware import require_admin
from src.image_backends import (
    ImageBackendError,
    list_a1111_checkpoints,
    list_a1111_loras,
    list_comfyui_checkpoints,
    list_comfyui_loras,
)

logger = logging.getLogger(__name__)

_URL_SETTING_KEYS = {
    "automatic1111": "image_backend_a1111_url",
    "sdnext": "image_backend_sdnext_url",
    "comfyui": "image_backend_comfyui_url",
}


def setup_image_backend_routes() -> APIRouter:
    router = APIRouter(prefix="/api/image-backends", tags=["image-backends"])

    def _backend_url(backend: str) -> str:
        from src.settings import get_setting
        key = _URL_SETTING_KEYS.get(backend)
        if not key:
            raise HTTPException(400, f"Unknown backend: {backend}")
        url = get_setting(key, "") or ""
        if not url:
            raise HTTPException(400, f"No server URL configured for {backend}")
        return url

    @router.get("/checkpoints")
    async def checkpoints(request: Request, backend: str = Query(...)):
        require_admin(request)
        url = _backend_url(backend)
        try:
            if backend == "comfyui":
                names = await list_comfyui_checkpoints(url)
                return {"checkpoints": names}
            models = await list_a1111_checkpoints(url)
            return {"checkpoints": [m["model_name"] or m["title"] for m in models]}
        except ImageBackendError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/loras")
    async def loras(request: Request, backend: str = Query(...)):
        require_admin(request)
        url = _backend_url(backend)
        try:
            if backend == "comfyui":
                return {"loras": await list_comfyui_loras(url)}
            return {"loras": await list_a1111_loras(url)}
        except ImageBackendError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router

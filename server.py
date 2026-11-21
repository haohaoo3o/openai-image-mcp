# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2", "httpx", "cryptography==44.0.3"]
# ///
"""Minimal MCP server for OpenAI-compatible image generation & editing APIs.

Wraps `POST {IMAGE_API_BASE_URL}/images/generations` and
`POST {IMAGE_API_BASE_URL}/images/edits` over stdio. Works with the official
OpenAI images API and with OpenAI-compatible relays / self-hosted gateways
that accept aspect-ratio `size` strings and an `upscale` level ("2k"/"4k").

Relay quirks that are baked in from real-world testing:

- `quality` is dropped automatically when `upscale="4k"` (relays reject the
  combination).
- `upscale="4k"` is rejected up front for non-square sizes (relays return
  errors); use `2k` for 16:9 / 2:3 / 9:16 etc.
- Generated files are measured after saving; if a 4k request silently fell
  back to a small render (width < 2000), the call is retried automatically
  (up to 2 times, configurable) and a warning is returned.
- Edits omit `size` by default so the output keeps the reference image's
  aspect ratio; pass `size` explicitly to override.

Images are saved to disk and the tool returns the absolute path plus the
measured pixel size — no inline base64 in the model context. Read the file
from the path when the image actually needs to be seen.

Not affiliated with OpenAI. Billing follows your provider / relay account.
"""

import base64
import os
import re
import struct
import sys
import time
import uuid

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("IMAGE_API_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("IMAGE_API_KEY", "")
DEFAULT_MODEL = os.environ.get("IMAGE_API_MODEL", "")
REQUEST_TIMEOUT_SECONDS = 300  # 4k renders can take a while

mcp = FastMCP("openai-image-mcp")


def _default_output_path() -> str:
    pictures = os.path.join(os.path.expanduser("~"), "Pictures", "image-gen-mcp")
    if not os.path.isdir(os.path.dirname(pictures)):
        pictures = os.path.expanduser("~/image-gen-mcp")
    os.makedirs(pictures, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(pictures, f"{stamp}-{uuid.uuid4().hex[:6]}.png")


def _is_square(size: str) -> bool:
    if not size or size == "1:1":
        return True
    m = re.fullmatch(r"(\d+)x(\d+)", size.strip())
    return bool(m) and m.group(1) == m.group(2)


def _png_size(data: bytes):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def _jpeg_size(data: bytes):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def _image_size(data: bytes):
    return _png_size(data) or _jpeg_size(data) or (None, None)


def _mime_for(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _error_payload(resp: httpx.Response) -> dict:
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text[:500]
    return {"error": {"status": resp.status_code, "message": detail}}


def _generate_once(client: httpx.Client, prompt: str, model: str, n: int,
                   size: str, upscale, quality, output_path: str) -> dict:
    body = {"model": model, "prompt": prompt, "n": n, "size": size,
            "response_format": "b64_json"}
    if upscale:
        body["upscale"] = upscale
    if quality and upscale != "4k":
        body["quality"] = quality
    resp = client.post("/images/generations", json=body)
    if resp.status_code != 200:
        return _error_payload(resp)
    payload = resp.json()
    items = [i for i in (payload.get("data") or []) if i.get("b64_json")]
    if items:
        data = base64.b64decode(items[0]["b64_json"])
        with open(output_path, "wb") as fh:
            fh.write(data)
        w, h = _image_size(data)
        result = {"ok": True, "saved_path": output_path, "bytes": len(data),
                  "width": w, "height": h, "images_returned": len(items)}
        if upscale == "4k" and w is not None and w < 2000:
            result["fallback"] = True
        return result
    urls = [i.get("url") for i in (payload.get("data") or []) if i.get("url")]
    if urls:
        return {"ok": True, "saved_path": None, "url": urls[0],
                "images_returned": len(urls),
                "note": "provider returned a URL instead of base64; "
                        "download it yourself"}
    return {"error": {"message": "Provider returned no image data",
                      "raw_keys": list(payload.keys())}}


@mcp.tool()
def generate_image(
    prompt: str,
    model: str = "",
    n: int = 1,
    size: str = "1:1",
    upscale: str = "",
    quality: str = "",
    output_path: str = "",
    retry_on_fallback: bool = True,
) -> dict:
    """Generate images from a text prompt via an OpenAI-compatible API.

    Args:
        prompt: Text prompt for the image.
        model: Model name. Defaults to the IMAGE_API_MODEL env var.
        n: Number of images; each one is billed by your provider.
        size: Aspect ratio ("1:1", "16:9", "2:3", "9:16") or explicit
            pixels ("1024x1024"). Non-square sizes must not use 4k.
        upscale: "" (none) / "2k" / "4k". 4k only for square sizes; the
            quality field is dropped automatically at 4k because relays
            reject the combination.
        quality: Optional quality tier (provider-specific); ignored at 4k.
        output_path: Absolute path to save the PNG. Defaults to
            ~/Pictures/image-gen-mcp/<timestamp>-<id>.png.
        retry_on_fallback: When upscale="4k" and the render comes back
            below 2000 px wide (relays occasionally fall back to 1024),
            retry the same generation up to 2 times. Each attempt is billed.
            The saved file is always the last attempt; earlier attempts are
            overwritten.
    """
    if not BASE_URL or not API_KEY:
        return {"error": {"message": "set IMAGE_API_BASE_URL and IMAGE_API_KEY "
                         "environment variables"}}
    model = model or DEFAULT_MODEL
    if not model:
        return {"error": {"message": "No model: pass model or set IMAGE_API_MODEL"}}
    if upscale and upscale not in ("2k", "4k"):
        return {"error": {"message": "upscale must be '2k', '4k', or empty"}}
    if upscale == "4k" and not _is_square(size):
        return {"error": {"message": "upscale='4k' is only accepted for square "
                         "sizes (1:1); use '2k' for 16:9 / 2:3 / 9:16 etc."}}
    output_path = output_path or _default_output_path()
    if not os.path.isabs(output_path):
        return {"error": {"message": "output_path must be absolute"}}

    retries = 2 if (retry_on_fallback and upscale == "4k") else 0
    result = {}
    with httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS,
                      headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        attempt = 0
        while True:
            result = _generate_once(client, prompt, model, n, size, upscale,
                                    quality, output_path)
            attempt += 1
            if "error" in result or not result.get("fallback"):
                break
            if attempt > retries:
                break
    if "error" not in result:
        result["attempts"] = attempt
        if result.get("fallback"):
            result["warning"] = ("4k render stayed below 2000 px wide after "
                                 f"{attempt} attempt(s); consider 2k or retry later")
    return result


@mcp.tool()
def edit_image(
    image_path: str,
    prompt: str,
    model: str = "",
    n: int = 1,
    size: str = "",
    upscale: str = "2k",
    mask_path: str = "",
    output_path: str = "",
) -> dict:
    """Edit a local image from a prompt via an OpenAI-compatible API.

    Args:
        image_path: Absolute path to the reference image (PNG/JPEG/WEBP/GIF).
        prompt: What to change or produce.
        model: Model name. Defaults to the IMAGE_API_MODEL env var. Pass it
            explicitly when your provider's edit default differs from its
            generation default.
        n: Number of images; each one is billed by your provider.
        size: Omit to keep the reference image's aspect ratio (recommended;
            providers that ignore size on edits still work). Pass "1:1" /
            "16:9" / explicit pixels to force a different output size.
        upscale: "" / "2k" / "4k". Defaults to "2k": 4k edits frequently hit
            upstream gateway timeouts on relays. Non-square + 4k is rejected
            up front.
        mask_path: Optional absolute path to a mask image (transparent =
            editable).
        output_path: Absolute path to save the result. Defaults to
            ~/Pictures/image-gen-mcp/<timestamp>-<id>.png.
    """
    if not BASE_URL or not API_KEY:
        return {"error": {"message": "set IMAGE_API_BASE_URL and IMAGE_API_KEY "
                         "environment variables"}}
    model = model or DEFAULT_MODEL
    if not model:
        return {"error": {"message": "No model: pass model or set IMAGE_API_MODEL"}}
    if upscale and upscale not in ("2k", "4k"):
        return {"error": {"message": "upscale must be '2k', '4k', or empty"}}
    if upscale == "4k" and size and not _is_square(size):
        return {"error": {"message": "upscale='4k' is only accepted for square sizes"}}
    try:
        with open(image_path, "rb") as fh:
            ref = fh.read()
    except OSError as exc:
        return {"error": {"message": f"cannot read image_path: {exc}"}}
    mask = b""
    if mask_path:
        try:
            with open(mask_path, "rb") as fh:
                mask = fh.read()
        except OSError as exc:
            return {"error": {"message": f"cannot read mask_path: {exc}"}}

    fields = {"model": model, "prompt": prompt, "n": str(n),
              "response_format": "b64_json"}
    if size:
        fields["size"] = size
    if upscale:
        fields["upscale"] = upscale
    files = {"image": (os.path.basename(image_path), ref, _mime_for(ref))}
    if mask:
        files["mask"] = (os.path.basename(mask_path), mask, _mime_for(mask))

    output_path = output_path or _default_output_path()
    with httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS,
                      headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        resp = client.post("/images/edits", data=fields, files=files)
    if resp.status_code != 200:
        return _error_payload(resp)
    payload = resp.json()
    items = [i for i in (payload.get("data") or []) if i.get("b64_json")]
    if not items:
        urls = [i.get("url") for i in (payload.get("data") or []) if i.get("url")]
        if urls:
            return {"ok": True, "saved_path": None, "url": urls[0],
                    "note": "provider returned a URL instead of base64"}
        return {"error": {"message": "Provider returned no image data",
                          "raw_keys": list(payload.keys())}}
    data = base64.b64decode(items[0]["b64_json"])
    with open(output_path, "wb") as fh:
        fh.write(data)
    w, h = _image_size(data)
    return {"ok": True, "saved_path": output_path, "bytes": len(data),
            "width": w, "height": h, "images_returned": len(items),
            "note": "edits default to the reference image's aspect ratio when "
                    "size is omitted"}


if __name__ == "__main__":
    if not BASE_URL or not API_KEY:
        print("IMAGE_API_BASE_URL and IMAGE_API_KEY environment variables are required",
              file=sys.stderr)
        sys.exit(1)
    mcp.run()

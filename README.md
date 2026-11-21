# openai-image-mcp

Minimal single-file MCP server for **OpenAI-compatible image generation & editing APIs** over stdio.

- Wraps `POST {base}/images/generations` (text-to-image) and `POST {base}/images/edits` (image editing, multipart)
- Works with the official OpenAI images API **and** with OpenAI-compatible relays / self-hosted gateways that accept aspect-ratio `size` strings and an `upscale` level (`2k` / `4k`)
- **Single file** — `server.py` with inline [PEP 723](https://peps.python.org/pep-0723/) dependencies (`cryptography` pinned to a prebuilt-friendly version so hosts without a Rust toolchain, e.g. Intel Macs, don't fail on source builds)
- **Returns file paths, not inline base64** — images are saved to disk and the tool answers with the absolute path plus the measured pixel size; your model context stays small and you only read the file when the image actually needs to be seen
- Relay quirks observed in real-world testing are built in (see below)

Not affiliated with OpenAI. Billing follows your provider / relay account.

## Relay behavior baked in from real-world testing

These rules come from verified behavior on OpenAI-compatible relays and are enforced or handled automatically:

| Rule | Behavior |
|---|---|
| `4k` + `quality` is rejected by relays | `quality` is dropped automatically whenever `upscale="4k"` |
| Non-square + `4k` is rejected | Rejected up front with a message pointing at `2k` (use `2k` for `16:9` / `2:3` / `9:16`) |
| Relay `4k` square renders measure ~2508×2508, not 4096 | Documented in the tool response |
| `4k` occasionally falls back to 1024×1024 under load | Saved file is measured; when width < 2000 px the call is retried automatically (up to 2 times, `retry_on_fallback=False` to disable — **each attempt is billed**) |
| `4k` edits frequently hit upstream gateway timeouts (HTTP 524) | `edit_image` defaults to `upscale="2k"` |
| Edit output follows the reference image's aspect ratio | `edit_image` omits `size` by default; pass it explicitly to override |

## Setup

Three environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `IMAGE_API_BASE_URL` | yes | Base URL that already includes the version segment, e.g. `https://api.openai.com/v1` or your relay's `/v1` endpoint |
| `IMAGE_API_KEY` | yes | API key (sent as `Authorization: Bearer ...`) |
| `IMAGE_API_MODEL` | recommended | Default model for both tools; each call can still pass `model` explicitly |

### Client configuration

Run-from-URL (no clone needed):

```json
{
  "mcpServers": {
    "openai-image-mcp": {
      "command": "uv",
      "args": [
        "run",
        "https://raw.githubusercontent.com/haohaoo3o/openai-image-mcp/main/server.py"
      ],
      "env": {
        "IMAGE_API_BASE_URL": "https://your-relay.example/v1",
        "IMAGE_API_KEY": "sk-...",
        "IMAGE_API_MODEL": "your-image-model"
      }
    }
  }
}
```

Or after cloning, replace the URL with `["run", "--no-project", "/absolute/path/to/openai-image-mcp/server.py"]`.

Files are saved to `~/Pictures/image-gen-mcp/<timestamp>-<id>.png` by default (falls back to `~/image-gen-mcp/` when `~/Pictures` does not exist); pass an absolute `output_path` to choose your own.

## Tools

### `generate_image`

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | required | |
| `model` | string | env `IMAGE_API_MODEL` | |
| `n` | int | 1 | each image is billed by your provider |
| `size` | string | `"1:1"` | aspect ratio or pixels (`"16:9"`, `"1024x1024"`); non-square cannot use `4k` |
| `upscale` | string | `""` | `""` / `"2k"` / `"4k"`; `quality` is auto-dropped at `4k` |
| `quality` | string | `""` | provider-specific tier; ignored at `4k` |
| `output_path` | string | auto | must be absolute |
| `retry_on_fallback` | bool | `true` | auto-retry `4k` renders that come back < 2000 px wide (max 2 retries, billed) |

Returns `{ok, saved_path, bytes, width, height, images_returned, attempts}` (plus `fallback`/`warning` when applicable).

### `edit_image`

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `image_path` | string | required | absolute path to the reference image |
| `prompt` | string | required | |
| `model` | string | env `IMAGE_API_MODEL` | pass explicitly if your provider's edit model differs |
| `n` | int | 1 | |
| `size` | string | `""` (omitted) | omitted = keep the reference image's aspect ratio |
| `upscale` | string | `"2k"` | `4k` edits often hit gateway timeouts on relays |
| `mask_path` | string | `""` | optional mask image |
| `output_path` | string | auto | must be absolute |

Both tools return provider errors as `{error: {status, message}}` instead of raising.

## Credits

Wire-format conventions (aspect-ratio `size` strings, `upscale` levels, b64 responses) were cross-checked against existing community servers in this space, notably [`@aaa147258qq/image-gen-mcp`](https://github.com/aaa147258qq/image-gen-mcp). This is an independent implementation written from scratch.

## License

[MIT](LICENSE)

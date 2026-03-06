# ai-auto-switch

Probe multiple Gemini-compatible `{GOOGLE_GEMINI_BASE_URL, GEMINI_API_KEY}` pairs,
pick the fastest healthy one, and activate it.

By default, a successful run writes the selected values to `~/.gemini/.env`.

Defaults are split:
- probe model for latency checks: `gemini-3-flash-preview`
- session model written to env: `gemini-3-pro-preview`

## What this does

- Tests connectivity/latency against each provider via
  `POST /v1beta/models/{model}:generateContent` (tiny ping payload) by default.
- Uses multiple attempts per provider and ranks by:
  - higher success count first
  - lower median latency second
- Probes providers in parallel and enforces a global runtime cap (`--total-timeout`).
- Uses two stages:
  - cheap probe first (fast, low cost)
  - expensive fallback probe only when all cheap results are failed or slower than 5000ms
- Activates the winner by:
  - printing shell exports, or
  - writing/updating an env file.

## Config

Copy the example:

```bash
cp providers.example.json providers.json
```

Then fill your providers in `providers.json`:

```json
{
  "providers": [
    {
      "name": "yunwu-main",
      "base_url": "https://yunwu.ai",
      "api_key_env": "YUNWU_MAIN_API_KEY",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview"
    },
    {
      "name": "relay-backup",
      "base_url": "https://another-relay.example",
      "api_key": "your-direct-key-here",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview"
    }
  ]
}
```

Recommended: use `api_key_env` instead of plain `api_key`.

## Usage

Run probe only:

```bash
python3 switch.py --config providers.json --attempts 2 --timeout 8 --no-auto-write
```

Default run (auto-updates `~/.gemini/.env`):

```bash
python3 switch.py
```

Cap total script runtime (default is 20s):

```bash
python3 switch.py --config providers.json --total-timeout 20
```

Tune/disable expensive fallback behavior:

```bash
python3 switch.py --expensive-threshold-ms 5000 --expensive-attempts 1
python3 switch.py --no-expensive-probe
```

Set/override model for all providers:

```bash
python3 switch.py --config providers.json --model gemini-3-flash-preview
```

Set/override real session model written to env:

```bash
python3 switch.py --config providers.json --session-model gemini-3-pro-preview
```

If a relay uses non-standard/self-signed TLS certs:

```bash
python3 switch.py --config providers.json --insecure
```

Specify a CA bundle explicitly (recommended over `--insecure`):

```bash
python3 switch.py --config providers.json --ca-file /etc/ssl/cert.pem
```

Activate in current shell:

```bash
eval "$(python3 switch.py --config providers.json --print-export)"
```

Write selected values into an env file:

```bash
python3 switch.py --config providers.json --write-env ~/.gemini/.env
```

## proxy_app.py (local proxy)

Install proxy runtime dependencies first:

```bash
python3 -m pip install --user -r requirements.txt
```

Default run (detached background process, menubar mode by default):

```bash
python3 proxy_app.py
```

- Log file default: `/tmp/ai-auto-switch-proxy.log`
- Foreground mode (stay attached to terminal):

```bash
python3 proxy_app.py --foreground
```

- Probe strategy in proxy mode:
  - Probe cheap providers first.
  - Run expensive probe stage only if all cheap results fail or are slower than `5000ms`.
- Optional tuning:

```bash
python3 proxy_app.py --probe-expensive-threshold-ms 5000 --probe-expensive-attempts 1
```

- Default logs are concise status lines:
  - `[probe-stage][cheap] WORKING healthy=3/7 fastest=uniapi-0.5 (1481.5ms)`
  - `[probe-stage][cheap] 01 yunwu-1=WORKING latency=1556.0ms`
  - `[probe-stage][cheap] 05 yunwu-2=NOT_WORKING latency=2101.3ms reason=HTTP 500: quota error ...`
  - `[probe-stage][expensive] SKIPPED reason=cheap_healthy_within_threshold threshold=5000.0ms`
  - `[probe-status] WORKING selected=uniapi-0.5 source=cheap latency=1481.5ms`
  - `[probe-status] NOT_WORKING reason=...`
- Enable per-provider details only when needed:

```bash
python3 proxy_app.py --probe-detail
```

If `rumps` is missing, the process logs a notice and falls back to headless mode. Install it with:

```bash
python3 -m pip install --user rumps
```

Headless-only run:

```bash
python3 proxy_app.py --headless
```

Health check:

```bash
curl -sS http://127.0.0.1:8080/_health
```

## Optional per-provider fields

- `test_path` (optional, overrides model-based probe URL)
- `test_method` (optional, default: `POST` for model probe, `GET` when `test_path` is set)
- `test_body` (optional custom body for POST/PUT/PATCH probe methods)
- `model` (probe model, default: `gemini-3-flash-preview`)
- `session_model` (exported runtime model, default: `gemini-3-pro-preview`)
- `cheap_only` (optional bool, only participate in cheap stage)
- `expensive_only` (optional bool, only participate in expensive stage)
- `use_query_key` (default: `true`)
- `use_header_key` (default: `true`)
- `header_key_name` (default: `x-goog-api-key`)
- `headers` (extra request headers object)

## Notes

- The script sets these env vars when activating:
  - `GOOGLE_GEMINI_BASE_URL`
  - `GEMINI_API_KEY`
  - `GOOGLE_GEMINI_API_KEY`
  - `GEMINI_MODEL`
  - `GOOGLE_GEMINI_MODEL`
- If all providers fail, the script exits non-zero.
- If Python's default CA store is missing, script auto-fallbacks to common CA bundle paths.

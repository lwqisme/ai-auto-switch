// TODO: balance系数是可以调的，后面看下怎么调它会更合理一点

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
      "input_price": 1.2,
      "base_url": "https://yunwu.ai",
      "api_key_env": "YUNWU_MAIN_API_KEY",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview"
    },
    {
      "name": "relay-backup",
      "input_price": 3.8,
      "base_url": "https://another-relay.example",
      "api_key": "your-direct-key-here",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview"
    }
  ]
}
```

Recommended: use `api_key_env` instead of plain `api_key`.
For `proxy_app.py` score-based routing, each provider must include `input_price`.

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
- By default, proxy mode auto-updates `~/.gemini/.env` with:
  - `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8080` (or your `--host/--port`)
  - selected provider key and session model env vars
- Disable auto-write:

```bash
python3 proxy_app.py --no-auto-write
```

- Write proxy env vars to a custom file:

```bash
python3 proxy_app.py --write-env ~/.gemini/.env
```

- Foreground mode (stay attached to terminal):

```bash
python3 proxy_app.py --foreground
```

- Probe strategy in proxy mode:
  - Background probe runs every `600s` (10 minutes) by default (`--probe-interval`).
  - Marks providers unhealthy on timeout, HTTP 5xx, or consecutive failures.
  - Maintains moving average latency using the last 5 successful pings (`--latency-window`).
  - Uses min-max normalized `input_price` + latency score:
    - `Balance_Score = (alpha * normalized_price) + ((1-alpha) * normalized_latency)`
    - Lower score is better.
  - Sticky selection: keeps current healthy provider unless a challenger is significantly better
    (default `20%` lower score via `--sticky-improvement-threshold`).
- Optional tuning:

```bash
python3 proxy_app.py --probe-interval 600 --alpha 0.5 --sticky-improvement-threshold 0.2
python3 proxy_app.py --latency-window 5 --failure-threshold 2
```

- Live request fallback:
  - If active provider fails on real traffic (timeout, rate-limit, 5xx), it is immediately
    marked unhealthy.
  - Proxy re-elects the next best healthy provider and retries the same request automatically.
- Default logs are concise status lines:
  - `[2026-03-07 14:20:00+0800] [probe-status] WORKING healthy=5/8 selected=foo score=0.1842 latency=132.1ms reason=sticky_keep`
  - `[2026-03-07 14:20:00+0800] [probe-provider] foo=WORKING avg_latency=132.1ms score=0.1842 fails=0`
  - `[2026-03-07 14:20:01+0800] [live] provider=foo failure=HTTP 500: ...`
  - `[2026-03-07 14:20:01+0800] [route] failover from=foo to=bar reason=select_best`
- Enable extra per-provider probe fields (probe result, input price, flags, error):

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

- `input_price` (required for `proxy_app.py` scoring)
- `test_path` (optional, overrides model-based probe URL)
- `test_method` (optional, default: `POST` for model probe, `GET` when `test_path` is set)
- `test_body` (optional custom body for POST/PUT/PATCH probe methods)
- `model` (probe model, default: `gemini-3-flash-preview`)
- `session_model` (exported runtime model, default: `gemini-3-pro-preview`)
- `cheap_only` (optional bool metadata/filter flag)
- `expensive_only` (optional bool metadata/filter flag)
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

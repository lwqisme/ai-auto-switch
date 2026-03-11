// TODO: balance系数是可以调的，后面看下怎么调它会更合理一点

# ai-auto-switch

[中文说明](README.md)

Probe multiple Gemini-compatible `{GOOGLE_GEMINI_BASE_URL, GEMINI_API_KEY}` pairs,
pick the fastest healthy one, and activate it.

By default, a successful run writes the selected values to `~/.gemini/.env`.

Defaults are split:
- probe model for latency checks: `gemini-3-flash-preview`
- session model written to env: `gemini-3-pro-preview`

## What this does

- Runs a local Gemini-compatible proxy with background health probing.
- Probes providers in two stages:
  - probes cheap-stage providers first (all providers where `expensive_only != true`)
  - probes expensive-stage providers only when all cheap-stage probes fail/timeout
- Ranks healthy providers by normalized `input_price` + latency score.
- Routes live requests to the selected healthy provider and retries on retryable failures.
- Auto-updates `~/.gemini/.env` by default with local proxy URL + selected provider creds/model.

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
  - `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:18080` (or your `--host/--port`)
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

- In menubar mode, the dropdown shows every provider with simple live status:
  - Rows are sorted by score (lowest/best first; unknown score at bottom).
  - Includes a `Probe Interval` submenu with live presets (`1m`, `5m`, `10m`, `30m`).
  - Shows `Probing...` only inside the dropdown while a probe cycle is running.
  - `⭐ 🟢 provider score=0.184 123ms` (active healthy provider)
  - `  🔴 provider DOWN` (currently unhealthy)
  - `  🟡 provider INIT` (not probed yet)
  - Includes `Quit` to exit the menubar app.

- Probe strategy in proxy mode:
  - Background probe runs every `60s` (1 minute) by default (`--probe-interval`).
  - Probes cheap-stage providers first (`expensive_only != true`).
  - Probes expensive-stage providers only when all cheap-stage probes fail/timeout.
  - Marks providers unhealthy on timeout, HTTP 5xx, or consecutive failures.
  - Maintains moving average latency using the last 5 successful pings (`--latency-window`).
  - Uses min-max normalized `input_price` + latency score:
    - `Balance_Score = (alpha * normalized_price) + ((1-alpha) * normalized_latency)`
    - Lower score is better.
  - Default selection always follows the lowest score (`--sticky-improvement-threshold 0.0`).
  - Optional sticky behavior: keep current healthy provider unless a challenger is significantly
    better (for example `20%` lower score via `--sticky-improvement-threshold 0.2`).
- Optional tuning:

```bash
python3 proxy_app.py --probe-interval 60 --alpha 0.5 --sticky-improvement-threshold 0.2
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
curl -sS http://127.0.0.1:18080/_health
```

## Optional per-provider fields

- `input_price` (required for `proxy_app.py` scoring)
- `test_path` (optional, overrides model-based probe URL)
- `test_method` (optional, default: `POST` for model probe, `GET` when `test_path` is set)
- `test_body` (optional custom body for POST/PUT/PATCH probe methods)
- `model` (probe model, default: `gemini-3-flash-preview`)
- `session_model` (exported runtime model, default: `gemini-3-pro-preview`)
- `cheap_only` (optional bool; excludes provider from expensive fallback stage)
- `expensive_only` (optional bool; excludes provider from cheap stage, used only in fallback)
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

import argparse
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import switch

DEFAULT_CONFIG_PATH = Path(__file__).with_name("providers.json")
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8080
DEFAULT_PROBE_INTERVAL_SECONDS = 60.0
DEFAULT_PROBE_ATTEMPTS = 1
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS = 10.0


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.best_provider: switch.Provider | None = None
        self.last_probe_error: str | None = None
        self.last_probe_time_unix: float | None = None
        self.last_probe_latency_ms: float | None = None

    def set_probe_result(
        self,
        provider: switch.Provider | None,
        error: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        with self._lock:
            self.best_provider = provider
            self.last_probe_error = error
            self.last_probe_time_unix = time.time()
            self.last_probe_latency_ms = latency_ms

    def snapshot(
        self,
    ) -> tuple[switch.Provider | None, str | None, float | None, float | None]:
        with self._lock:
            return (
                self.best_provider,
                self.last_probe_error,
                self.last_probe_time_unix,
                self.last_probe_latency_ms,
            )


APP_STATE = RuntimeState()
PROVIDERS: list[switch.Provider] = []
PROBE_INTERVAL_SECONDS = DEFAULT_PROBE_INTERVAL_SECONDS
PROBE_ATTEMPTS = DEFAULT_PROBE_ATTEMPTS
PROBE_TIMEOUT_SECONDS = DEFAULT_PROBE_TIMEOUT_SECONDS
PROBE_TOTAL_TIMEOUT_SECONDS = DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS
PROBE_INSECURE = False
PROBE_CA_FILE: str | None = None

app = FastAPI(title="AI Auto Switch Proxy")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local proxy for selected best provider.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Providers config path (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_PROXY_HOST,
        help=f"Proxy bind host (default: {DEFAULT_PROXY_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PROXY_PORT,
        help=f"Proxy bind port (default: {DEFAULT_PROXY_PORT}).",
    )
    parser.add_argument(
        "--probe-interval",
        type=float,
        default=DEFAULT_PROBE_INTERVAL_SECONDS,
        help=f"Seconds between background probes (default: {DEFAULT_PROBE_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--probe-attempts",
        type=int,
        default=DEFAULT_PROBE_ATTEMPTS,
        help=f"Attempts per provider in background probe (default: {DEFAULT_PROBE_ATTEMPTS}).",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT_SECONDS,
        help=f"Per-request timeout in background probe (default: {DEFAULT_PROBE_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--probe-total-timeout",
        type=float,
        default=DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS,
        help=(
            "Total timeout in seconds for each background probe run "
            f"(default: {DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification during background probes.",
    )
    parser.add_argument(
        "--ca-file",
        default=None,
        help="CA bundle for probe requests. Defaults to switch.py auto-resolution.",
    )
    parser.add_argument(
        "--menubar",
        action="store_true",
        help=(
            "Enable macOS menubar status app (default behavior). "
            "Kept for compatibility."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Disable menubar and run proxy server only.",
    )
    return parser.parse_args()


def load_runtime_providers(config_path: str) -> list[switch.Provider]:
    return switch.load_providers(
        Path(config_path).expanduser(),
        None,
        switch.DEFAULT_TEST_MODEL,
        switch.DEFAULT_SESSION_MODEL,
    )


def run_probe_once() -> switch.ProbeSummary | None:
    if not PROVIDERS:
        APP_STATE.set_probe_result(None, "No providers configured.")
        return None

    deadline = time.monotonic() + PROBE_TOTAL_TIMEOUT_SECONDS
    summaries = switch.probe_all_providers_parallel(
        providers=PROVIDERS,
        attempts=PROBE_ATTEMPTS,
        timeout_s=PROBE_TIMEOUT_SECONDS,
        insecure=PROBE_INSECURE,
        ca_file=PROBE_CA_FILE,
        deadline=deadline,
    )
    ranked = switch.rank_summaries(summaries)
    selected = next((item for item in ranked if item.is_healthy), None)
    if selected:
        APP_STATE.set_probe_result(
            selected.provider, None, latency_ms=selected.median_latency_ms
        )
    else:
        errors = [item.last_error for item in ranked if item.last_error]
        APP_STATE.set_probe_result(
            None, errors[0] if errors else "No healthy provider.", latency_ms=None
        )
    return selected


def prober_loop() -> None:
    while True:
        try:
            result = run_probe_once()
            if result:
                median = int(result.median_latency_ms) if result.median_latency_ms else "?"
                print(f"[probe] selected={result.provider.name} latency={median}ms", flush=True)
            else:
                _, err, _, _ = APP_STATE.snapshot()
                print(f"[probe] no healthy provider: {err}", flush=True)
        except Exception as err:  # pragma: no cover
            APP_STATE.set_probe_result(None, f"Probe exception: {type(err).__name__}: {err}")
            print(f"[probe] exception: {err}", flush=True)
        time.sleep(PROBE_INTERVAL_SECONDS)


def _sanitize_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out = dict(headers)
    out.pop("transfer-encoding", None)
    out.pop("connection", None)
    return out


async def _proxy_stream_generator(
    client: httpx.AsyncClient, response: httpx.Response
):  # pragma: no cover
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()


@app.get("/_health")
async def health() -> JSONResponse:
    provider, err, ts, latency_ms = APP_STATE.snapshot()
    payload = {
        "best_provider": provider.name if provider else None,
        "base_url": provider.base_url if provider else None,
        "last_probe_error": err,
        "last_probe_time_unix": ts,
        "last_probe_latency_ms": latency_ms,
    }
    return JSONResponse(payload)


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
async def proxy_handler(request: Request, path: str):
    provider, err, _, _ = APP_STATE.snapshot()
    if not provider:
        return JSONResponse(
            {
                "error": "No healthy provider selected yet. Please wait a moment.",
                "detail": err,
            },
            status_code=503,
        )

    base = provider.base_url.rstrip("/")
    target_url = f"{base}/{path.lstrip('/')}"
    params = dict(request.query_params)
    if provider.use_query_key:
        params["key"] = provider.api_key

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["accept-encoding"] = "identity"
    if provider.use_header_key:
        headers[provider.header_key_name] = provider.api_key

    body = await request.body()
    client = httpx.AsyncClient(timeout=120.0)
    req = client.build_request(
        method=request.method,
        url=target_url,
        params=params,
        headers=headers,
        content=body,
    )

    response = await client.send(req, stream=True)
    proxy_headers = _sanitize_response_headers(response.headers)
    return StreamingResponse(
        _proxy_stream_generator(client, response),
        status_code=response.status_code,
        headers=proxy_headers,
    )


def run_optional_menubar() -> bool:
    try:
        import rumps
    except Exception as err:
        print(f"[menubar] disabled: failed to import rumps ({err})", flush=True)
        return False

    class ProxyMenuBarApp(rumps.App):
        def __init__(self):
            super().__init__("🤖 Proxy")
            self.menu = [
                rumps.MenuItem("Force Probe Now", callback=self.force_probe),
                rumps.separator,
                rumps.MenuItem("Status in terminal", callback=None),
            ]
            self._refresh_title()
            self._timer = rumps.Timer(self._refresh_title, 2)
            self._timer.start()

        def _refresh_title(self, _=None):
            provider, err, _, latency_ms = APP_STATE.snapshot()
            if provider:
                if latency_ms is not None:
                    self.title = f"🤖 {provider.name} ({int(latency_ms)}ms)"
                else:
                    self.title = f"🤖 {provider.name}"
            elif err:
                self.title = "🤖 Error"
            else:
                self.title = "🤖 Init"

        def force_probe(self, _):
            run_probe_once()
            self._refresh_title()

    try:
        ProxyMenuBarApp().run()
        return True
    except Exception as err:
        print(f"[menubar] disabled: runtime error ({err})", flush=True)
        return False


def run_uvicorn_server(host: str, port: int) -> None:
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> int:
    global PROVIDERS
    global PROBE_INTERVAL_SECONDS
    global PROBE_ATTEMPTS
    global PROBE_TIMEOUT_SECONDS
    global PROBE_TOTAL_TIMEOUT_SECONDS
    global PROBE_INSECURE
    global PROBE_CA_FILE

    args = parse_args()
    if args.headless and args.menubar:
        print("Use only one of --headless or --menubar.")
        return 2

    menubar_enabled = not args.headless
    if args.probe_interval <= 0:
        print("--probe-interval must be > 0")
        return 2
    if args.probe_attempts <= 0:
        print("--probe-attempts must be > 0")
        return 2
    if args.probe_timeout <= 0:
        print("--probe-timeout must be > 0")
        return 2
    if args.probe_total_timeout <= 0:
        print("--probe-total-timeout must be > 0")
        return 2

    try:
        PROVIDERS = load_runtime_providers(args.config)
    except Exception as err:
        print(f"Failed to load providers: {err}")
        return 2

    PROBE_INTERVAL_SECONDS = args.probe_interval
    PROBE_ATTEMPTS = args.probe_attempts
    PROBE_TIMEOUT_SECONDS = args.probe_timeout
    PROBE_TOTAL_TIMEOUT_SECONDS = args.probe_total_timeout
    PROBE_INSECURE = args.insecure
    try:
        PROBE_CA_FILE = None if PROBE_INSECURE else switch.resolve_ca_file(args.ca_file)
    except Exception as err:
        print(f"Invalid TLS config: {err}")
        return 2

    run_probe_once()
    threading.Thread(target=prober_loop, daemon=True).start()

    if menubar_enabled:
        print("[menubar] enabled (default mode).", flush=True)
        # rumps must run on the main thread.
        threading.Thread(
            target=run_uvicorn_server, args=(args.host, args.port), daemon=True
        ).start()
        menubar_started = run_optional_menubar()
        if menubar_started:
            return 0
        print("[menubar] falling back to headless server mode.", flush=True)

    run_uvicorn_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

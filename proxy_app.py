import argparse
import subprocess
import sys
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
DEFAULT_PROBE_INTERVAL_SECONDS = 900.0
DEFAULT_PROBE_ATTEMPTS = 1
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS = 10.0
DEFAULT_PROBE_EXPENSIVE_ATTEMPTS = switch.DEFAULT_EXPENSIVE_ATTEMPTS
DEFAULT_PROBE_EXPENSIVE_THRESHOLD_MS = switch.DEFAULT_EXPENSIVE_THRESHOLD_MS
DEFAULT_BACKGROUND_LOG_FILE = "/tmp/ai-auto-switch-proxy.log"
RUMPS_INSTALL_CMD = "python3 -m pip install --user rumps"


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
PROBE_EXPENSIVE_ATTEMPTS = DEFAULT_PROBE_EXPENSIVE_ATTEMPTS
PROBE_EXPENSIVE_THRESHOLD_MS = DEFAULT_PROBE_EXPENSIVE_THRESHOLD_MS
PROBE_DETAIL = False
PROBE_INSECURE = False
PROBE_CA_FILE: str | None = None

app = FastAPI(title="AI Auto Switch Proxy")


def print_rumps_install_hint() -> None:
    print(f"[menubar] to enable menubar mode, run: {RUMPS_INSTALL_CMD}", flush=True)


def _compact_error(error: str | None, limit: int = 160) -> str | None:
    if not error:
        return None
    one_line = " ".join(error.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _format_median(median_latency_ms: float | None) -> str:
    if median_latency_ms is None:
        return "-"
    return f"{median_latency_ms:.1f}ms"


def _format_latency_list(latencies_ms: list[float]) -> str:
    if not latencies_ms:
        return "-"
    return ", ".join(f"{latency:.1f}ms" for latency in latencies_ms)


def _provider_status_word(item: switch.ProbeSummary) -> str:
    return "WORKING" if item.is_healthy else "NOT_WORKING"


def log_probe_stage_summary(stage: str, ranked: list[switch.ProbeSummary]) -> None:
    if not ranked:
        print(f"[probe-stage][{stage}] NOT_WORKING providers=0", flush=True)
        return

    healthy_count = sum(1 for item in ranked if item.is_healthy)
    fastest = next((item for item in ranked if item.is_healthy), None)
    if fastest:
        fastest_info = (
            f"{fastest.provider.name} ({_format_median(fastest.median_latency_ms)})"
        )
        status = "WORKING"
    else:
        fastest_info = "none"
        status = "NOT_WORKING"

    print(
        f"[probe-stage][{stage}] {status} "
        f"healthy={healthy_count}/{len(ranked)} fastest={fastest_info}",
        flush=True,
    )

    for index, item in enumerate(ranked, start=1):
        status_word = _provider_status_word(item)
        latency = _format_median(item.median_attempt_latency_ms)
        line = (
            f"[probe-stage][{stage}] {index:02d} "
            f"{item.provider.name}={status_word} latency={latency}"
        )
        if not item.is_healthy:
            reason = _compact_error(item.last_error)
            if reason:
                line += f" reason={reason}"
        print(line, flush=True)


def log_probe_details(stage: str, ranked: list[switch.ProbeSummary]) -> None:
    if not PROBE_DETAIL:
        return
    if not ranked:
        return

    name_width = max(len(item.provider.name) for item in ranked)
    for index, item in enumerate(ranked, start=1):
        health = "OK" if item.is_healthy else "FAIL"
        median_success = _format_median(item.median_latency_ms)
        median_attempt = _format_median(item.median_attempt_latency_ms)
        latencies = _format_latency_list(item.attempt_latencies_ms)
        print(
            f"[probe-detail][{stage}] {index:02d} "
            f"{item.provider.name:<{name_width}} "
            f"status={health:<4} success={item.success_count}/{item.attempts} "
            f"median_ok={median_success:<9} median_try={median_attempt:<9} "
            f"attempt_latencies=[{latencies}]",
            flush=True,
        )
        compact_error = _compact_error(item.last_error)
        if compact_error:
            print(
                f"[probe-detail][{stage}]    error[{item.provider.name}]: {compact_error}",
                flush=True,
            )


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
        "--probe-expensive-attempts",
        type=int,
        default=DEFAULT_PROBE_EXPENSIVE_ATTEMPTS,
        help=(
            "Attempts per provider for expensive fallback probe "
            f"(default: {DEFAULT_PROBE_EXPENSIVE_ATTEMPTS})."
        ),
    )
    parser.add_argument(
        "--probe-expensive-threshold-ms",
        type=float,
        default=DEFAULT_PROBE_EXPENSIVE_THRESHOLD_MS,
        help=(
            "Run expensive probe only when cheap probes are all failed "
            f"or slower than this ms threshold (default: {DEFAULT_PROBE_EXPENSIVE_THRESHOLD_MS})."
        ),
    )
    parser.add_argument(
        "--probe-detail",
        action="store_true",
        help="Print per-provider probe details (default: summary only).",
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
    parser.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Run attached to terminal. "
            "Default behavior starts a detached background process."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_BACKGROUND_LOG_FILE,
        help=(
            "Background process log file "
            f"(default: {DEFAULT_BACKGROUND_LOG_FILE})."
        ),
    )
    return parser.parse_args()


def launch_background_process(args: argparse.Namespace) -> int:
    log_path = Path(args.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--foreground",
        "--config",
        args.config,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--probe-interval",
        str(args.probe_interval),
        "--probe-attempts",
        str(args.probe_attempts),
        "--probe-timeout",
        str(args.probe_timeout),
        "--probe-total-timeout",
        str(args.probe_total_timeout),
        "--probe-expensive-attempts",
        str(args.probe_expensive_attempts),
        "--probe-expensive-threshold-ms",
        str(args.probe_expensive_threshold_ms),
    ]
    if args.probe_detail:
        cmd.append("--probe-detail")
    if args.insecure:
        cmd.append("--insecure")
    if args.ca_file:
        cmd.extend(["--ca-file", args.ca_file])
    if args.headless:
        cmd.append("--headless")
    elif args.menubar:
        cmd.append("--menubar")

    with log_path.open("ab") as log_fp:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(f"[proxy] started in background: pid={process.pid}")
    print(f"[proxy] log file: {log_path}")
    print(f"[proxy] health: curl -sS http://{args.host}:{args.port}/_health")
    return 0


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

    cheap_providers = [item for item in PROVIDERS if not item.expensive_only]
    expensive_providers = [item for item in PROVIDERS if not item.cheap_only]
    if not cheap_providers and not expensive_providers:
        APP_STATE.set_probe_result(None, "No providers enabled for probing.")
        return None

    deadline = time.monotonic() + PROBE_TOTAL_TIMEOUT_SECONDS
    cheap_summaries = switch.probe_all_providers_parallel(
        providers=cheap_providers,
        attempts=PROBE_ATTEMPTS,
        timeout_s=PROBE_TIMEOUT_SECONDS,
        insecure=PROBE_INSECURE,
        ca_file=PROBE_CA_FILE,
        deadline=deadline,
    )
    cheap_ranked = switch.rank_summaries(cheap_summaries)
    log_probe_stage_summary("cheap", cheap_ranked)
    log_probe_details("cheap", cheap_ranked)
    cheap_selected = next((item for item in cheap_ranked if item.is_healthy), None)

    selected = cheap_selected
    selected_source = "cheap" if cheap_selected else None
    expensive_ranked: list[switch.ProbeSummary] | None = None

    should_run_expensive = (
        bool(expensive_providers)
        and switch.should_run_expensive_probe(
            cheap_ranked, PROBE_EXPENSIVE_THRESHOLD_MS
        )
    )
    if should_run_expensive:
        print(
            "[probe-stage][expensive] TRIGGERED "
            f"reason=cheap_failed_or_slow threshold={PROBE_EXPENSIVE_THRESHOLD_MS:.1f}ms",
            flush=True,
        )
        expensive_summaries = switch.probe_all_providers_parallel(
            providers=switch.build_expensive_providers(expensive_providers),
            attempts=PROBE_EXPENSIVE_ATTEMPTS,
            timeout_s=PROBE_TIMEOUT_SECONDS,
            insecure=PROBE_INSECURE,
            ca_file=PROBE_CA_FILE,
            deadline=deadline,
        )
        expensive_ranked = switch.rank_summaries(expensive_summaries)
        log_probe_stage_summary("expensive", expensive_ranked)
        log_probe_details("expensive", expensive_ranked)
        expensive_selected = next(
            (item for item in expensive_ranked if item.is_healthy), None
        )
        if expensive_selected:
            selected = expensive_selected
            selected_source = "expensive"
        elif selected:
            selected_source = "cheap_fallback"
            print(
                "[probe-stage][expensive] FALLBACK "
                "reason=no_healthy_expensive using=cheap_winner",
                flush=True,
            )
    elif cheap_ranked:
        print(
            "[probe-stage][expensive] SKIPPED "
            f"reason=cheap_healthy_within_threshold threshold={PROBE_EXPENSIVE_THRESHOLD_MS:.1f}ms",
            flush=True,
        )
    else:
        print("[probe-stage][expensive] SKIPPED reason=no_providers", flush=True)

    if selected:
        selected_latency = _format_median(selected.median_latency_ms)
        print(
            f"[probe-status] WORKING selected={selected.provider.name} "
            f"source={selected_source} latency={selected_latency}",
            flush=True,
        )
        APP_STATE.set_probe_result(
            selected.provider, None, latency_ms=selected.median_latency_ms
        )
    else:
        ranked_error_sources = []
        if expensive_ranked is not None:
            ranked_error_sources.append(expensive_ranked)
        ranked_error_sources.append(cheap_ranked)
        errors = [
            item.last_error
            for ranked in ranked_error_sources
            for item in ranked
            if item.last_error
        ]
        APP_STATE.set_probe_result(
            None, errors[0] if errors else "No healthy provider.", latency_ms=None
        )
        print(
            f"[probe-status] NOT_WORKING reason={_compact_error(errors[0] if errors else 'No healthy provider.')}",
            flush=True,
        )
    return selected


def prober_loop() -> None:
    while True:
        try:
            run_probe_once()
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
        print_rumps_install_hint()
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
    global PROBE_EXPENSIVE_ATTEMPTS
    global PROBE_EXPENSIVE_THRESHOLD_MS
    global PROBE_DETAIL
    global PROBE_INSECURE
    global PROBE_CA_FILE

    args = parse_args()
    if args.headless and args.menubar:
        print("Use only one of --headless or --menubar.")
        return 2
    if not args.foreground:
        return launch_background_process(args)

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
    if args.probe_expensive_attempts <= 0:
        print("--probe-expensive-attempts must be > 0")
        return 2
    if args.probe_expensive_threshold_ms < 0:
        print("--probe-expensive-threshold-ms must be >= 0")
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
    PROBE_EXPENSIVE_ATTEMPTS = args.probe_expensive_attempts
    PROBE_EXPENSIVE_THRESHOLD_MS = args.probe_expensive_threshold_ms
    PROBE_DETAIL = bool(args.probe_detail)
    PROBE_INSECURE = args.insecure
    try:
        PROBE_CA_FILE = None if PROBE_INSECURE else switch.resolve_ca_file(args.ca_file)
    except Exception as err:
        print(f"Invalid TLS config: {err}")
        return 2

    run_probe_once()
    threading.Thread(target=prober_loop, daemon=True).start()

    if menubar_enabled:
        try:
            import rumps as _rumps  # noqa: F401
        except Exception as err:
            print(f"[menubar] disabled: failed to import rumps ({err})", flush=True)
            print_rumps_install_hint()
            print("[menubar] falling back to headless server mode.", flush=True)
        else:
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

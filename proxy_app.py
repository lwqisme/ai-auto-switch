import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import switch

DEFAULT_CONFIG_PATH = Path(__file__).with_name("providers.json")
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8080
DEFAULT_PROBE_INTERVAL_SECONDS = 600.0
DEFAULT_PROBE_ATTEMPTS = 1
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS = 10.0
DEFAULT_SCORE_ALPHA = 0.7
DEFAULT_STICKY_IMPROVEMENT_THRESHOLD = 0.2
DEFAULT_LATENCY_WINDOW = 5
DEFAULT_FAILURE_THRESHOLD = 2
DEFAULT_BACKGROUND_LOG_FILE = "/tmp/ai-auto-switch-proxy.log"
DEFAULT_PROXY_ENV_FILE = switch.DEFAULT_GEMINI_ENV_FILE
RUMPS_INSTALL_CMD = "python3 -m pip install --user rumps"

RETRYABLE_LIVE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
HTTP_STATUS_PATTERN = re.compile(r"HTTP\s+(\d{3})")


@dataclass
class ProviderRuntime:
    provider: switch.Provider
    input_price: float
    is_healthy: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_probe_latency_ms: float | None = None
    moving_avg_latency_ms: float | None = None
    balance_score: float | None = None
    success_latencies_ms: list[float] = field(default_factory=list)
    last_probe_time_unix: float | None = None


RUNTIME_LOCK = threading.Lock()
RUNTIME_BY_NAME: dict[str, ProviderRuntime] = {}
RUNTIME_ORDER: list[str] = []
ACTIVE_PROVIDER_NAME: str | None = None
LAST_PROBE_ERROR: str | None = None
LAST_PROBE_TIME_UNIX: float | None = None

PROBE_INTERVAL_SECONDS = DEFAULT_PROBE_INTERVAL_SECONDS
PROBE_ATTEMPTS = DEFAULT_PROBE_ATTEMPTS
PROBE_TIMEOUT_SECONDS = DEFAULT_PROBE_TIMEOUT_SECONDS
PROBE_TOTAL_TIMEOUT_SECONDS = DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS
PROBE_DETAIL = False
PROBE_INSECURE = False
PROBE_CA_FILE: str | None = None
SCORE_ALPHA = DEFAULT_SCORE_ALPHA
STICKY_IMPROVEMENT_THRESHOLD = DEFAULT_STICKY_IMPROVEMENT_THRESHOLD
LATENCY_WINDOW_SIZE = DEFAULT_LATENCY_WINDOW
FAILURE_THRESHOLD = DEFAULT_FAILURE_THRESHOLD

ENV_WRITE_TARGET: Path | None = None
PROXY_PUBLIC_BASE_URL: str | None = None
LAST_WRITTEN_ENV: dict[str, str] | None = None
ENV_WRITE_LOCK = threading.Lock()

app = FastAPI(title="AI Auto Switch Proxy")


def print_rumps_install_hint() -> None:
    _log(f"[menubar] to enable menubar mode, run: {RUMPS_INSTALL_CMD}")


def _log(message: str) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{ts}] {message}", flush=True)


def _compact_error(error: str | None, limit: int = 160) -> str | None:
    if not error:
        return None
    one_line = " ".join(error.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}ms"


def build_public_proxy_base_url(host: str, port: int) -> str:
    resolved_host = host.strip()
    if resolved_host in {"0.0.0.0", "::", "[::]"}:
        # Bind-only hosts are not useful as client-facing URLs.
        resolved_host = "127.0.0.1"
    if ":" in resolved_host and not resolved_host.startswith("["):
        resolved_host = f"[{resolved_host}]"
    return f"http://{resolved_host}:{port}"


def maybe_write_proxy_env(provider: switch.Provider) -> None:
    global LAST_WRITTEN_ENV
    if not ENV_WRITE_TARGET or not PROXY_PUBLIC_BASE_URL:
        return

    selected_env = {
        "GOOGLE_GEMINI_BASE_URL": PROXY_PUBLIC_BASE_URL,
        "GEMINI_API_KEY": provider.api_key,
        "GOOGLE_GEMINI_API_KEY": provider.api_key,
        "GEMINI_MODEL": provider.session_model,
        "GOOGLE_GEMINI_MODEL": provider.session_model,
    }

    with ENV_WRITE_LOCK:
        if LAST_WRITTEN_ENV == selected_env:
            return
        switch.write_env_file(ENV_WRITE_TARGET, selected_env)
        LAST_WRITTEN_ENV = dict(selected_env)

    _log(
        f"[env] updated file={ENV_WRITE_TARGET} "
        f"base_url={PROXY_PUBLIC_BASE_URL} "
        f"provider={provider.name} key={switch.mask_key(provider.api_key)}"
    )


def _load_raw_provider_items(config_path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ValueError(f"Config file not found: {config_path}") from err
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON in config: {config_path} ({err})") from err

    if isinstance(raw, dict):
        items = raw.get("providers")
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("Config must be either a list or an object with a providers list.")

    if not isinstance(items, list) or not items:
        raise ValueError("Providers list is empty.")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Provider entry #{index} must be a JSON object.")
        normalized.append(item)
    return normalized


def load_runtime_providers(config_path: str) -> list[ProviderRuntime]:
    path = Path(config_path).expanduser()
    raw_items = _load_raw_provider_items(path)
    parsed_providers = switch.load_providers(
        path,
        None,
        switch.DEFAULT_TEST_MODEL,
        switch.DEFAULT_SESSION_MODEL,
    )
    if len(parsed_providers) != len(raw_items):
        raise ValueError("Internal provider parse mismatch.")

    runtimes: list[ProviderRuntime] = []
    seen_names: set[str] = set()
    for index, (provider, raw_item) in enumerate(zip(parsed_providers, raw_items), start=1):
        if provider.name in seen_names:
            raise ValueError(f'Duplicate provider name "{provider.name}" is not supported.')
        seen_names.add(provider.name)

        if "input_price" not in raw_item:
            raise ValueError(
                f'Provider "{provider.name}" missing required field: input_price '
                f"(entry #{index})."
            )
        try:
            input_price = float(raw_item["input_price"])
        except (TypeError, ValueError) as err:
            raise ValueError(
                f'Provider "{provider.name}" has invalid input_price: '
                f"{raw_item['input_price']!r}"
            ) from err
        if input_price < 0:
            raise ValueError(
                f'Provider "{provider.name}" must use input_price >= 0.'
            )

        runtimes.append(ProviderRuntime(provider=provider, input_price=input_price))

    return runtimes


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
        help=f"Attempts per provider for each probe cycle (default: {DEFAULT_PROBE_ATTEMPTS}).",
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
            "Total timeout in seconds for each background probe cycle "
            f"(default: {DEFAULT_PROBE_TOTAL_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_SCORE_ALPHA,
        help=(
            "Score weight for normalized price (0.0..1.0). "
            f"Latency weight is (1-alpha). Default: {DEFAULT_SCORE_ALPHA}."
        ),
    )
    parser.add_argument(
        "--sticky-improvement-threshold",
        type=float,
        default=DEFAULT_STICKY_IMPROVEMENT_THRESHOLD,
        help=(
            "Switch active provider only when challenger score is at least this "
            "fraction lower than active score (default: "
            f"{DEFAULT_STICKY_IMPROVEMENT_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--latency-window",
        type=int,
        default=DEFAULT_LATENCY_WINDOW,
        help=(
            "Moving-average window size for successful probe latencies "
            f"(default: {DEFAULT_LATENCY_WINDOW})."
        ),
    )
    parser.add_argument(
        "--failure-threshold",
        type=int,
        default=DEFAULT_FAILURE_THRESHOLD,
        help=(
            "Mark provider unhealthy after this many consecutive failed probe cycles "
            f"(default: {DEFAULT_FAILURE_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--probe-detail",
        action="store_true",
        help="Print per-provider probe details.",
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
    parser.add_argument(
        "--write-env",
        default=None,
        help=(
            "Write selected proxy env vars into this file "
            "(e.g. ~/.gemini/.env)."
        ),
    )
    parser.add_argument(
        "--no-auto-write",
        action="store_true",
        help=(
            f"Do not auto-write selected proxy env vars to {DEFAULT_PROXY_ENV_FILE} "
            "when --write-env is absent."
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
        "--alpha",
        str(args.alpha),
        "--sticky-improvement-threshold",
        str(args.sticky_improvement_threshold),
        "--latency-window",
        str(args.latency_window),
        "--failure-threshold",
        str(args.failure_threshold),
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
    if args.write_env:
        cmd.extend(["--write-env", args.write_env])
    if args.no_auto_write:
        cmd.append("--no-auto-write")

    with log_path.open("ab") as log_fp:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _log(f"[proxy] started in background: pid={process.pid}")
    _log(f"[proxy] log file: {log_path}")
    _log(f"[proxy] health: curl -sS http://{args.host}:{args.port}/_health")
    return 0


def _normalize(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    return (value - min_value) / (max_value - min_value)


def _error_is_timeout(error: str | None) -> bool:
    if not error:
        return False
    lower = error.lower()
    return "timeout" in lower or "timed out" in lower


def _error_http_status(error: str | None) -> int | None:
    if not error:
        return None
    match = HTTP_STATUS_PATTERN.search(error)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _error_is_http_5xx(error: str | None) -> bool:
    status = _error_http_status(error)
    return bool(status is not None and 500 <= status < 600)


def _is_significantly_better(candidate_score: float, active_score: float) -> bool:
    if candidate_score >= active_score:
        return False
    if active_score <= 0:
        return candidate_score < active_score
    improvement = (active_score - candidate_score) / active_score
    return improvement >= STICKY_IMPROVEMENT_THRESHOLD


def _healthy_runtimes_locked() -> list[ProviderRuntime]:
    out: list[ProviderRuntime] = []
    for name in RUNTIME_ORDER:
        item = RUNTIME_BY_NAME[name]
        if item.is_healthy and item.moving_avg_latency_ms is not None:
            out.append(item)
    return out


def _recompute_scores_locked() -> list[ProviderRuntime]:
    for name in RUNTIME_ORDER:
        RUNTIME_BY_NAME[name].balance_score = None

    healthy = _healthy_runtimes_locked()
    if not healthy:
        return []

    prices = [item.input_price for item in healthy]
    latencies = [item.moving_avg_latency_ms for item in healthy if item.moving_avg_latency_ms is not None]
    if not latencies:
        return []

    min_price = min(prices)
    max_price = max(prices)
    min_latency = min(latencies)
    max_latency = max(latencies)

    for item in healthy:
        assert item.moving_avg_latency_ms is not None
        normalized_price = _normalize(item.input_price, min_price, max_price)
        normalized_latency = _normalize(item.moving_avg_latency_ms, min_latency, max_latency)
        item.balance_score = (SCORE_ALPHA * normalized_price) + ((1.0 - SCORE_ALPHA) * normalized_latency)

    return healthy


def _elect_active_provider_locked() -> tuple[ProviderRuntime | None, bool, str]:
    global ACTIVE_PROVIDER_NAME

    healthy = _recompute_scores_locked()
    previous_name = ACTIVE_PROVIDER_NAME
    previous = RUNTIME_BY_NAME.get(previous_name) if previous_name else None

    if not healthy:
        ACTIVE_PROVIDER_NAME = None
        changed = previous_name is not None
        return None, changed, "none_healthy"

    ranked = sorted(
        healthy,
        key=lambda item: (
            item.balance_score if item.balance_score is not None else float("inf"),
            item.moving_avg_latency_ms if item.moving_avg_latency_ms is not None else float("inf"),
            item.input_price,
            item.provider.name,
        ),
    )
    best = ranked[0]

    if previous and previous.is_healthy and previous.balance_score is not None:
        if (
            best.provider.name != previous.provider.name
            and best.balance_score is not None
            and _is_significantly_better(best.balance_score, previous.balance_score)
        ):
            ACTIVE_PROVIDER_NAME = best.provider.name
            return best, True, "switch_better_score"
        ACTIVE_PROVIDER_NAME = previous.provider.name
        return previous, False, "sticky_keep"

    ACTIVE_PROVIDER_NAME = best.provider.name
    changed = previous_name != ACTIVE_PROVIDER_NAME
    return best, changed, "select_best"


def _initialize_runtime(providers: list[ProviderRuntime]) -> None:
    global RUNTIME_BY_NAME
    global RUNTIME_ORDER
    global ACTIVE_PROVIDER_NAME
    global LAST_PROBE_ERROR
    global LAST_PROBE_TIME_UNIX

    with RUNTIME_LOCK:
        RUNTIME_BY_NAME = {item.provider.name: item for item in providers}
        RUNTIME_ORDER = [item.provider.name for item in providers]
        ACTIVE_PROVIDER_NAME = None
        LAST_PROBE_ERROR = None
        LAST_PROBE_TIME_UNIX = None


def _log_probe_cycle(result_by_name: dict[str, tuple[bool, float | None, str | None]], selection_reason: str) -> None:
    with RUNTIME_LOCK:
        total = len(RUNTIME_ORDER)
        healthy = [item for item in _healthy_runtimes_locked()]
        active = RUNTIME_BY_NAME.get(ACTIVE_PROVIDER_NAME) if ACTIVE_PROVIDER_NAME else None

        if active and active.balance_score is not None:
            _log(
                "[probe-status] WORKING "
                f"healthy={len(healthy)}/{total} "
                f"selected={active.provider.name} "
                f"score={active.balance_score:.4f} "
                f"latency={_format_ms(active.moving_avg_latency_ms)} "
                f"reason={selection_reason}"
            )
        elif healthy:
            _log(
                "[probe-status] WORKING "
                f"healthy={len(healthy)}/{total} "
                "selected=none "
                f"reason={selection_reason}"
            )
        else:
            _log(
                "[probe-status] NOT_WORKING "
                f"healthy=0/{total} reason={_compact_error(LAST_PROBE_ERROR) or 'No healthy provider.'}"
            )

        for name in RUNTIME_ORDER:
            item = RUNTIME_BY_NAME[name]
            probe_ok, probe_latency, probe_error = result_by_name.get(name, (False, None, "No probe result"))
            state_word = "WORKING" if item.is_healthy else "NOT_WORKING"
            score = "-" if item.balance_score is None else f"{item.balance_score:.4f}"
            line = (
                f"[probe-provider] {item.provider.name}={state_word} "
                f"avg_latency={_format_ms(item.moving_avg_latency_ms)} "
                f"score={score} "
                f"fails={item.consecutive_failures}"
            )
            if PROBE_DETAIL:
                line += (
                    f" probe_ok={probe_ok} "
                    f"probe_latency={_format_ms(probe_latency)} "
                    f"input_price={item.input_price:.6g} "
                    f"cheap_only={item.provider.cheap_only} "
                    f"expensive_only={item.provider.expensive_only}"
                )
                compact_probe_error = _compact_error(probe_error)
                if compact_probe_error and not probe_ok:
                    line += f" error={compact_probe_error}"
            _log(line)


def _probe_provider_cycle(provider: switch.Provider) -> tuple[bool, float | None, str | None]:
    last_latency: float | None = None
    last_error: str | None = None
    for _ in range(PROBE_ATTEMPTS):
        ok, latency_ms, error = switch.probe_once(
            provider,
            timeout_s=PROBE_TIMEOUT_SECONDS,
            insecure=PROBE_INSECURE,
            ca_file=PROBE_CA_FILE,
        )
        last_latency = latency_ms
        last_error = error
        if ok:
            return True, latency_ms, None
    return False, last_latency, last_error or "Probe failed"


def run_probe_once() -> switch.Provider | None:
    global ACTIVE_PROVIDER_NAME
    global LAST_PROBE_ERROR
    global LAST_PROBE_TIME_UNIX

    with RUNTIME_LOCK:
        names = list(RUNTIME_ORDER)
        providers = [RUNTIME_BY_NAME[name].provider for name in names]

    if not providers:
        with RUNTIME_LOCK:
            LAST_PROBE_TIME_UNIX = time.time()
            LAST_PROBE_ERROR = "No providers configured."
            ACTIVE_PROVIDER_NAME = None
        return None

    deadline = time.monotonic() + PROBE_TOTAL_TIMEOUT_SECONDS
    result_by_name: dict[str, tuple[bool, float | None, str | None]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(providers))) as executor:
        futures: dict[concurrent.futures.Future[tuple[bool, float | None, str | None]], str] = {}
        for name, provider in zip(names, providers):
            futures[executor.submit(_probe_provider_cycle, provider)] = name

        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = concurrent.futures.wait(futures.keys(), timeout=remaining)

        for future in done:
            name = futures[future]
            try:
                result_by_name[name] = future.result()
            except Exception as err:
                result_by_name[name] = (False, None, f"Probe exception: {type(err).__name__}: {err}")

        for future in not_done:
            name = futures[future]
            future.cancel()
            result_by_name[name] = (False, None, "Probe total timeout exceeded")

    env_update_provider: switch.Provider | None = None
    selection_reason = "none"
    selected_provider: switch.Provider | None = None
    now_unix = time.time()

    with RUNTIME_LOCK:
        LAST_PROBE_TIME_UNIX = now_unix

        for name in names:
            runtime = RUNTIME_BY_NAME[name]
            ok, latency_ms, error = result_by_name.get(name, (False, None, "No probe result"))

            runtime.last_probe_time_unix = now_unix
            runtime.last_probe_latency_ms = latency_ms

            if ok:
                runtime.is_healthy = True
                runtime.consecutive_failures = 0
                runtime.last_error = None
                if latency_ms is not None:
                    runtime.success_latencies_ms.append(latency_ms)
                    if len(runtime.success_latencies_ms) > LATENCY_WINDOW_SIZE:
                        runtime.success_latencies_ms = runtime.success_latencies_ms[-LATENCY_WINDOW_SIZE:]
                if runtime.success_latencies_ms:
                    runtime.moving_avg_latency_ms = (
                        sum(runtime.success_latencies_ms) / len(runtime.success_latencies_ms)
                    )
                else:
                    runtime.moving_avg_latency_ms = None
                continue

            runtime.consecutive_failures += 1
            runtime.last_error = error or "Probe failed"

            immediate_unhealthy = _error_is_timeout(runtime.last_error) or _error_is_http_5xx(
                runtime.last_error
            )
            if immediate_unhealthy or runtime.consecutive_failures >= FAILURE_THRESHOLD:
                runtime.is_healthy = False
                runtime.balance_score = None

        selected_runtime, changed, selection_reason = _elect_active_provider_locked()

        if selected_runtime:
            selected_provider = selected_runtime.provider
            LAST_PROBE_ERROR = None
            if changed:
                env_update_provider = selected_runtime.provider
        else:
            errors = [
                item.last_error
                for item in (RUNTIME_BY_NAME[name] for name in RUNTIME_ORDER)
                if item.last_error
            ]
            LAST_PROBE_ERROR = errors[0] if errors else "No healthy provider."

    _log_probe_cycle(result_by_name, selection_reason)

    if env_update_provider:
        maybe_write_proxy_env(env_update_provider)
    return selected_provider


def prober_loop() -> None:
    while True:
        try:
            run_probe_once()
        except Exception as err:  # pragma: no cover
            global LAST_PROBE_ERROR
            with RUNTIME_LOCK:
                LAST_PROBE_ERROR = f"Probe exception: {type(err).__name__}: {err}"
            _log(f"[probe] exception: {err}")
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


def _build_health_payload() -> dict[str, Any]:
    with RUNTIME_LOCK:
        active = RUNTIME_BY_NAME.get(ACTIVE_PROVIDER_NAME) if ACTIVE_PROVIDER_NAME else None
        providers_payload = []
        for name in RUNTIME_ORDER:
            item = RUNTIME_BY_NAME[name]
            providers_payload.append(
                {
                    "name": item.provider.name,
                    "base_url": item.provider.base_url,
                    "input_price": item.input_price,
                    "is_healthy": item.is_healthy,
                    "consecutive_failures": item.consecutive_failures,
                    "last_error": item.last_error,
                    "last_probe_latency_ms": item.last_probe_latency_ms,
                    "moving_avg_latency_ms": item.moving_avg_latency_ms,
                    "balance_score": item.balance_score,
                    "cheap_only": item.provider.cheap_only,
                    "expensive_only": item.provider.expensive_only,
                }
            )

        payload = {
            # Keep legacy keys for compatibility.
            "best_provider": active.provider.name if active else None,
            "base_url": active.provider.base_url if active else None,
            "last_probe_error": LAST_PROBE_ERROR,
            "last_probe_time_unix": LAST_PROBE_TIME_UNIX,
            "last_probe_latency_ms": active.moving_avg_latency_ms if active else None,
            # New routing metadata.
            "active_provider": active.provider.name if active else None,
            "score_alpha": SCORE_ALPHA,
            "sticky_improvement_threshold": STICKY_IMPROVEMENT_THRESHOLD,
            "latency_window": LATENCY_WINDOW_SIZE,
            "failure_threshold": FAILURE_THRESHOLD,
            "providers": providers_payload,
        }
    return payload


def _select_provider_for_request() -> tuple[switch.Provider | None, str | None, switch.Provider | None]:
    env_update_provider: switch.Provider | None = None
    with RUNTIME_LOCK:
        active = RUNTIME_BY_NAME.get(ACTIVE_PROVIDER_NAME) if ACTIVE_PROVIDER_NAME else None
        if active and active.is_healthy:
            return active.provider, None, None

        selected_runtime, changed, selection_reason = _elect_active_provider_locked()
        if selected_runtime:
            if changed:
                env_update_provider = selected_runtime.provider
                _log(f"[route] selected={selected_runtime.provider.name} reason={selection_reason}")
            return selected_runtime.provider, None, env_update_provider

        error = LAST_PROBE_ERROR or "No healthy provider selected yet."
        return None, error, None


def _mark_provider_unhealthy_from_live_failure(
    provider_name: str, error: str
) -> tuple[switch.Provider | None, switch.Provider | None]:
    global LAST_PROBE_ERROR
    env_update_provider: switch.Provider | None = None

    with RUNTIME_LOCK:
        runtime = RUNTIME_BY_NAME.get(provider_name)
        if runtime:
            runtime.is_healthy = False
            runtime.consecutive_failures = max(runtime.consecutive_failures + 1, FAILURE_THRESHOLD)
            runtime.last_error = error
            runtime.balance_score = None
            runtime.last_probe_time_unix = time.time()

        LAST_PROBE_ERROR = error
        selected_runtime, changed, selection_reason = _elect_active_provider_locked()
        if selected_runtime and changed:
            env_update_provider = selected_runtime.provider
            _log(
                f"[route] failover from={provider_name} to={selected_runtime.provider.name} "
                f"reason={selection_reason}"
            )
        elif selected_runtime is None:
            _log(f"[route] no healthy providers after failure from={provider_name}")

        selected_provider = selected_runtime.provider if selected_runtime else None

    return selected_provider, env_update_provider


def _is_retryable_live_status(status_code: int) -> bool:
    return status_code in RETRYABLE_LIVE_STATUS_CODES or status_code >= 500


async def _read_error_excerpt(response: httpx.Response, limit: int = 200) -> str:
    try:
        content = await response.aread()
    except Exception:
        return ""
    text = content.decode("utf-8", errors="ignore").strip()
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


async def _send_request_to_provider(
    request: Request,
    path: str,
    body: bytes,
    provider: switch.Provider,
) -> tuple[httpx.AsyncClient | None, httpx.Response | None, str | None]:
    base = provider.base_url.rstrip("/")
    target_url = f"{base}/{path.lstrip('/')}"

    params = list(request.query_params.multi_items())
    if provider.use_query_key:
        params.append(("key", provider.api_key))

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["accept-encoding"] = "identity"
    if provider.use_header_key:
        headers[provider.header_key_name] = provider.api_key
    if provider.headers:
        headers.update(provider.headers)

    client = httpx.AsyncClient(timeout=120.0)
    try:
        req = client.build_request(
            method=request.method,
            url=target_url,
            params=params,
            headers=headers,
            content=body,
        )
        response = await client.send(req, stream=True)
        return client, response, None
    except Exception as err:
        await client.aclose()
        return None, None, f"{type(err).__name__}: {err}"


@app.get("/_health")
async def health() -> JSONResponse:
    return JSONResponse(_build_health_payload())


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
async def proxy_handler(request: Request, path: str):
    body = await request.body()

    with RUNTIME_LOCK:
        max_attempts = max(1, len(RUNTIME_ORDER))

    attempts = 0
    last_error: str | None = None

    while attempts < max_attempts:
        provider, err, env_update_provider = _select_provider_for_request()
        if env_update_provider:
            maybe_write_proxy_env(env_update_provider)

        if not provider:
            return JSONResponse(
                {
                    "error": "No healthy provider selected yet. Please wait a moment.",
                    "detail": err,
                },
                status_code=503,
            )

        attempts += 1
        client, response, send_error = await _send_request_to_provider(
            request=request,
            path=path,
            body=body,
            provider=provider,
        )

        if send_error:
            last_error = send_error
            _log(f"[live] provider={provider.name} failure={_compact_error(send_error)}")
            next_provider, env_update_provider = _mark_provider_unhealthy_from_live_failure(
                provider.name,
                send_error,
            )
            if env_update_provider:
                maybe_write_proxy_env(env_update_provider)
            if not next_provider:
                break
            continue

        assert client is not None
        assert response is not None

        if _is_retryable_live_status(response.status_code):
            excerpt = await _read_error_excerpt(response)
            failure = f"HTTP {response.status_code}"
            if excerpt:
                failure = f"{failure}: {excerpt}"
            last_error = failure
            _log(f"[live] provider={provider.name} failure={_compact_error(failure)}")
            await response.aclose()
            await client.aclose()
            next_provider, env_update_provider = _mark_provider_unhealthy_from_live_failure(
                provider.name,
                failure,
            )
            if env_update_provider:
                maybe_write_proxy_env(env_update_provider)
            if not next_provider:
                break
            continue

        proxy_headers = _sanitize_response_headers(response.headers)
        return StreamingResponse(
            _proxy_stream_generator(client, response),
            status_code=response.status_code,
            headers=proxy_headers,
        )

    return JSONResponse(
        {
            "error": "All providers failed during request.",
            "detail": _compact_error(last_error) or "No healthy providers available.",
        },
        status_code=503,
    )


def run_optional_menubar() -> bool:
    try:
        import rumps
    except Exception as err:
        _log(f"[menubar] disabled: failed to import rumps ({err})")
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
            with RUNTIME_LOCK:
                active = RUNTIME_BY_NAME.get(ACTIVE_PROVIDER_NAME) if ACTIVE_PROVIDER_NAME else None
                error = LAST_PROBE_ERROR

            if active:
                if active.moving_avg_latency_ms is not None:
                    self.title = f"🤖 {active.provider.name} ({int(active.moving_avg_latency_ms)}ms)"
                else:
                    self.title = f"🤖 {active.provider.name}"
            elif error:
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
        _log(f"[menubar] disabled: runtime error ({err})")
        return False


def run_uvicorn_server(host: str, port: int) -> None:
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> int:
    global PROBE_INTERVAL_SECONDS
    global PROBE_ATTEMPTS
    global PROBE_TIMEOUT_SECONDS
    global PROBE_TOTAL_TIMEOUT_SECONDS
    global PROBE_DETAIL
    global PROBE_INSECURE
    global PROBE_CA_FILE
    global SCORE_ALPHA
    global STICKY_IMPROVEMENT_THRESHOLD
    global LATENCY_WINDOW_SIZE
    global FAILURE_THRESHOLD
    global ENV_WRITE_TARGET
    global PROXY_PUBLIC_BASE_URL
    global LAST_WRITTEN_ENV

    args = parse_args()
    if args.headless and args.menubar:
        _log("Use only one of --headless or --menubar.")
        return 2
    if not args.foreground:
        return launch_background_process(args)

    menubar_enabled = not args.headless
    if args.probe_interval <= 0:
        _log("--probe-interval must be > 0")
        return 2
    if args.probe_attempts <= 0:
        _log("--probe-attempts must be > 0")
        return 2
    if args.probe_timeout <= 0:
        _log("--probe-timeout must be > 0")
        return 2
    if args.probe_total_timeout <= 0:
        _log("--probe-total-timeout must be > 0")
        return 2
    if not (0.0 <= args.alpha <= 1.0):
        _log("--alpha must be between 0.0 and 1.0")
        return 2
    if not (0.0 <= args.sticky_improvement_threshold < 1.0):
        _log("--sticky-improvement-threshold must be between 0.0 and <1.0")
        return 2
    if args.latency_window <= 0:
        _log("--latency-window must be > 0")
        return 2
    if args.failure_threshold <= 0:
        _log("--failure-threshold must be > 0")
        return 2

    try:
        runtimes = load_runtime_providers(args.config)
    except Exception as err:
        _log(f"Failed to load providers: {err}")
        return 2
    _initialize_runtime(runtimes)

    PROBE_INTERVAL_SECONDS = args.probe_interval
    PROBE_ATTEMPTS = args.probe_attempts
    PROBE_TIMEOUT_SECONDS = args.probe_timeout
    PROBE_TOTAL_TIMEOUT_SECONDS = args.probe_total_timeout
    PROBE_DETAIL = bool(args.probe_detail)
    PROBE_INSECURE = args.insecure
    SCORE_ALPHA = args.alpha
    STICKY_IMPROVEMENT_THRESHOLD = args.sticky_improvement_threshold
    LATENCY_WINDOW_SIZE = args.latency_window
    FAILURE_THRESHOLD = args.failure_threshold
    try:
        PROBE_CA_FILE = None if PROBE_INSECURE else switch.resolve_ca_file(args.ca_file)
    except Exception as err:
        _log(f"Invalid TLS config: {err}")
        return 2

    env_write_target: str | None = args.write_env
    auto_write = False
    if not env_write_target and not args.no_auto_write:
        env_write_target = DEFAULT_PROXY_ENV_FILE
        auto_write = True
    if env_write_target:
        ENV_WRITE_TARGET = Path(env_write_target).expanduser()
        PROXY_PUBLIC_BASE_URL = build_public_proxy_base_url(args.host, args.port)
        LAST_WRITTEN_ENV = None
        if auto_write:
            _log(
                f"[env] auto-write enabled file={ENV_WRITE_TARGET} "
                f"base_url={PROXY_PUBLIC_BASE_URL}"
            )
        else:
            _log(
                f"[env] write target file={ENV_WRITE_TARGET} "
                f"base_url={PROXY_PUBLIC_BASE_URL}"
            )
    else:
        ENV_WRITE_TARGET = None
        PROXY_PUBLIC_BASE_URL = None
        LAST_WRITTEN_ENV = None

    run_probe_once()
    threading.Thread(target=prober_loop, daemon=True).start()

    if menubar_enabled:
        try:
            import rumps as _rumps  # noqa: F401
        except Exception as err:
            _log(f"[menubar] disabled: failed to import rumps ({err})")
            print_rumps_install_hint()
            _log("[menubar] falling back to headless server mode.")
        else:
            _log("[menubar] enabled (default mode).")
            # rumps must run on the main thread.
            threading.Thread(
                target=run_uvicorn_server, args=(args.host, args.port), daemon=True
            ).start()
            menubar_started = run_optional_menubar()
            if menubar_started:
                return 0
            _log("[menubar] falling back to headless server mode.")

    run_uvicorn_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

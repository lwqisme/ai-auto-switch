#!/usr/bin/env python3
"""Probe Gemini-compatible endpoints and activate the fastest healthy provider."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TEST_PATH = "/v1beta/models"
DEFAULT_TEST_MODEL = "gemini-3-flash-preview"
DEFAULT_SESSION_MODEL = "gemini-3-pro-preview"
DEFAULT_TIMEOUT = 8.0
DEFAULT_TOTAL_TIMEOUT = 20.0
DEFAULT_ATTEMPTS = 2
DEFAULT_EXPENSIVE_ATTEMPTS = 1
DEFAULT_EXPENSIVE_THRESHOLD_MS = 5000.0
DEFAULT_GEMINI_ENV_FILE = "~/.gemini/.env"
DEFAULT_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/usr/local/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
)
ENV_KEYS_TO_SET = (
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GOOGLE_GEMINI_MODEL",
)
SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9._:/@%+=-]+$")


class ConfigError(Exception):
    pass


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str = DEFAULT_TEST_MODEL
    session_model: str = DEFAULT_SESSION_MODEL
    cheap_only: bool = False
    expensive_only: bool = False
    test_path: str | None = None
    test_method: str = "POST"
    test_body: Any | None = None
    use_query_key: bool = True
    use_header_key: bool = True
    header_key_name: str = "x-goog-api-key"
    headers: dict[str, str] | None = None


@dataclass
class ProbeSummary:
    provider: Provider
    attempts: int
    success_count: int
    latencies_ms: list[float]
    attempt_latencies_ms: list[float]
    last_error: str | None

    @property
    def median_latency_ms(self) -> float | None:
        if not self.latencies_ms:
            return None
        return float(statistics.median(self.latencies_ms))

    @property
    def median_attempt_latency_ms(self) -> float | None:
        if not self.attempt_latencies_ms:
            return None
        return float(statistics.median(self.attempt_latencies_ms))

    @property
    def is_healthy(self) -> bool:
        return self.success_count > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test Gemini endpoint connectivity/latency across multiple "
            "{base_url, api_key} pairs and select the fastest healthy one."
        )
    )
    parser.add_argument(
        "--config",
        default="providers.json",
        help="Path to providers JSON config (default: providers.json).",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=DEFAULT_ATTEMPTS,
        help=f"Probe attempts per provider (default: {DEFAULT_ATTEMPTS}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout seconds for each probe (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--total-timeout",
        type=float,
        default=DEFAULT_TOTAL_TIMEOUT,
        help=f"Max total seconds for the entire run (default: {DEFAULT_TOTAL_TIMEOUT}).",
    )
    parser.add_argument(
        "--expensive-attempts",
        type=int,
        default=DEFAULT_EXPENSIVE_ATTEMPTS,
        help=(
            "Attempts per provider for expensive fallback probe "
            f"(default: {DEFAULT_EXPENSIVE_ATTEMPTS})."
        ),
    )
    parser.add_argument(
        "--expensive-threshold-ms",
        type=float,
        default=DEFAULT_EXPENSIVE_THRESHOLD_MS,
        help=(
            "Trigger expensive fallback probe when all cheap probes are failed "
            f"or slower than this threshold in ms (default: {DEFAULT_EXPENSIVE_THRESHOLD_MS})."
        ),
    )
    parser.add_argument(
        "--no-expensive-probe",
        action="store_true",
        help="Disable the expensive fallback probe stage.",
    )
    parser.add_argument(
        "--test-path",
        default=None,
        help=(
            "Default probe path when not set per provider. "
            "If omitted, the script probes /v1beta/models/{model}:generateContent."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_TEST_MODEL,
        help=f"Test model when using model probe path (default: {DEFAULT_TEST_MODEL}).",
    )
    parser.add_argument(
        "--session-model",
        default=DEFAULT_SESSION_MODEL,
        help=f"Model exported for real Gemini sessions (default: {DEFAULT_SESSION_MODEL}).",
    )
    parser.add_argument(
        "--write-env",
        help="Write selected provider env vars into this file (e.g. ~/.gemini/.env).",
    )
    parser.add_argument(
        "--no-auto-write",
        action="store_true",
        help=(
            "Do not auto-write selected env vars to ~/.gemini/.env "
            "when --write-env and --print-export are both absent."
        ),
    )
    parser.add_argument(
        "--print-export",
        action="store_true",
        help="Print shell export commands for the selected provider.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit probe result JSON summary in addition to human-readable output.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for probe requests.",
    )
    parser.add_argument(
        "--ca-file",
        default=None,
        help=(
            "Path to CA bundle file for TLS verification. "
            "If omitted, script uses Python defaults and auto-fallbacks."
        ),
    )
    return parser.parse_args()


def load_providers(
    config_path: Path,
    default_test_path: str | None,
    default_model: str,
    default_session_model: str,
) -> list[Provider]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config: {config_path} ({exc})") from exc

    if isinstance(raw, dict):
        items = raw.get("providers")
        if items is None:
            raise ConfigError('Config object must include a "providers" array.')
    elif isinstance(raw, list):
        items = raw
    else:
        raise ConfigError("Config must be either a list or an object with a providers list.")

    if not isinstance(items, list) or not items:
        raise ConfigError("Providers list is empty.")

    providers: list[Provider] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Provider entry #{idx} must be a JSON object.")

        name = str(item.get("name") or f"provider-{idx}")
        base_url = str(item.get("base_url") or "").strip()
        if not base_url:
            raise ConfigError(f'Provider "{name}" missing required field: base_url')

        api_key = str(item.get("api_key") or "").strip()
        api_key_env = str(item.get("api_key_env") or "").strip()
        if not api_key and api_key_env:
            api_key = os.getenv(api_key_env, "").strip()
        if not api_key:
            hint = f' or set env var "{api_key_env}"' if api_key_env else ""
            raise ConfigError(
                f'Provider "{name}" missing api_key{hint}. '
                "Use api_key or api_key_env in config."
            )

        model = str(item.get("model") or default_model).strip()
        if not model:
            raise ConfigError(f'Provider "{name}" missing model.')

        session_model = str(item.get("session_model") or default_session_model).strip()
        if not session_model:
            raise ConfigError(f'Provider "{name}" missing session_model.')

        cheap_only = bool(item.get("cheap_only", False))
        expensive_only = bool(item.get("expensive_only", False))
        if cheap_only and expensive_only:
            raise ConfigError(
                f'Provider "{name}" cannot set both cheap_only and expensive_only.'
            )

        raw_test_path = item.get("test_path", default_test_path)
        test_path: str | None = None
        if raw_test_path is not None:
            test_path_str = str(raw_test_path).strip()
            if test_path_str:
                test_path = test_path_str

        raw_test_method = item.get("test_method")
        if raw_test_method is None:
            test_method = "GET" if test_path else "POST"
        else:
            test_method = str(raw_test_method).strip().upper()
        if test_method not in {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE"}:
            raise ConfigError(
                f'Provider "{name}" has unsupported test_method "{test_method}".'
            )

        test_body = item.get("test_body")
        if test_body is not None and test_method not in {"POST", "PUT", "PATCH"}:
            raise ConfigError(
                f'Provider "{name}" sets test_body but test_method "{test_method}" does not use a body.'
            )

        headers = item.get("headers") or {}
        if not isinstance(headers, dict):
            raise ConfigError(f'Provider "{name}" field "headers" must be an object.')

        provider = Provider(
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            session_model=session_model,
            cheap_only=cheap_only,
            expensive_only=expensive_only,
            test_path=test_path,
            test_method=test_method,
            test_body=test_body,
            use_query_key=bool(item.get("use_query_key", True)),
            use_header_key=bool(item.get("use_header_key", True)),
            header_key_name=str(item.get("header_key_name") or "x-goog-api-key"),
            headers={str(k): str(v) for k, v in headers.items()},
        )
        providers.append(provider)

    return providers


def resolve_probe_path(provider: Provider) -> str:
    if provider.test_path:
        return (
            provider.test_path
            if provider.test_path.startswith("/")
            else f"/{provider.test_path}"
        )

    model_name = provider.model.strip()
    if model_name.startswith("models/"):
        model_name = model_name[len("models/") :]
    model_name = urllib.parse.quote(model_name, safe="._-")
    return f"/v1beta/models/{model_name}:generateContent"


def build_probe_url(provider: Provider) -> str:
    base = provider.base_url.rstrip("/")
    path = resolve_probe_path(provider)
    url = f"{base}{path}"
    if provider.use_query_key:
        sep = "&" if "?" in url else "?"
        key_qs = urllib.parse.urlencode({"key": provider.api_key})
        url = f"{url}{sep}{key_qs}"
    return url


def default_generate_probe_body() -> dict[str, Any]:
    return {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }


def default_expensive_generate_probe_body() -> dict[str, Any]:
    return {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Return a concise two-sentence response about network latency "
                            "testing reliability."
                        )
                    }
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 96, "temperature": 0},
    }


def should_run_expensive_probe(
    cheap_ranked: list[ProbeSummary], expensive_threshold_ms: float
) -> bool:
    if not cheap_ranked:
        return True
    for item in cheap_ranked:
        if not item.is_healthy:
            continue
        median = item.median_latency_ms
        if median is not None and median <= expensive_threshold_ms:
            return False
    return True


def build_expensive_providers(providers: list[Provider]) -> list[Provider]:
    expensive_body = default_expensive_generate_probe_body()
    return [
        Provider(
            name=item.name,
            base_url=item.base_url,
            api_key=item.api_key,
            model=item.session_model,
            session_model=item.session_model,
            cheap_only=item.cheap_only,
            expensive_only=item.expensive_only,
            test_path=None,
            test_method="POST",
            test_body=expensive_body,
            use_query_key=item.use_query_key,
            use_header_key=item.use_header_key,
            header_key_name=item.header_key_name,
            headers=dict(item.headers or {}),
        )
        for item in providers
    ]


def resolve_ca_file(user_ca_file: str | None) -> str | None:
    if user_ca_file:
        path = Path(user_ca_file).expanduser()
        if not path.is_file():
            raise ConfigError(f"CA bundle file not found: {path}")
        return str(path)

    paths = ssl.get_default_verify_paths()
    cafile_ok = bool(paths.cafile and Path(paths.cafile).is_file())
    capath_ok = bool(paths.capath and Path(paths.capath).is_dir())
    if cafile_ok or capath_ok:
        return None

    for candidate in DEFAULT_CA_BUNDLE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def probe_once(
    provider: Provider, timeout_s: float, insecure: bool, ca_file: str | None
) -> tuple[bool, float | None, str | None]:
    url = build_probe_url(provider)
    headers: dict[str, str] = {
        "User-Agent": "ai-auto-switch/1.0",
        "Accept": "application/json",
    }
    if provider.use_header_key:
        headers[provider.header_key_name] = provider.api_key
    if provider.headers:
        headers.update(provider.headers)

    request_body: bytes | None = None
    if provider.test_method in {"POST", "PUT", "PATCH"}:
        body = provider.test_body
        if body is None and provider.test_path is None:
            body = default_generate_probe_body()
        if body is not None:
            request_body = json.dumps(body, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url=url, data=request_body, method=provider.test_method, headers=headers
    )
    ssl_context: ssl.SSLContext | None = None
    if insecure:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    elif ca_file:
        ssl_context = ssl.create_default_context(cafile=ca_file)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_context) as resp:
            _ = resp.read(64)
            status = getattr(resp, "status", 200)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if 200 <= status < 300:
                return True, elapsed_ms, None
            return False, elapsed_ms, f"HTTP {status}"
    except urllib.error.HTTPError as err:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        body = err.read(120).decode("utf-8", errors="ignore").strip()
        extra = f": {body}" if body else ""
        return False, elapsed_ms, f"HTTP {err.code}{extra}"
    except urllib.error.URLError as err:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return False, elapsed_ms, f"URL error: {err.reason}"
    except TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return False, elapsed_ms, "Timeout"
    except Exception as err:  # pragma: no cover - conservative fallback
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return False, elapsed_ms, f"{type(err).__name__}: {err}"


def probe_provider(
    provider: Provider,
    attempts: int,
    timeout_s: float,
    insecure: bool,
    ca_file: str | None,
    deadline: float | None,
) -> ProbeSummary:
    latencies: list[float] = []
    attempt_latencies: list[float] = []
    last_error: str | None = None
    success_count = 0

    for _ in range(attempts):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = "Skipped: total timeout exceeded"
                break
            attempt_timeout = min(timeout_s, remaining)
        else:
            attempt_timeout = timeout_s

        ok, latency_ms, error = probe_once(provider, attempt_timeout, insecure, ca_file)
        if latency_ms is not None:
            attempt_latencies.append(latency_ms)
        if ok:
            success_count += 1
            if latency_ms is not None:
                latencies.append(latency_ms)
        else:
            last_error = error or "Unknown error"

    return ProbeSummary(
        provider=provider,
        attempts=attempts,
        success_count=success_count,
        latencies_ms=latencies,
        attempt_latencies_ms=attempt_latencies,
        last_error=last_error,
    )


def make_skipped_summary(provider: Provider, attempts: int, reason: str) -> ProbeSummary:
    return ProbeSummary(
        provider=provider,
        attempts=attempts,
        success_count=0,
        latencies_ms=[],
        attempt_latencies_ms=[],
        last_error=reason,
    )


def probe_all_providers_parallel(
    providers: list[Provider],
    attempts: int,
    timeout_s: float,
    insecure: bool,
    ca_file: str | None,
    deadline: float | None,
) -> list[ProbeSummary]:
    ordered_summaries: list[ProbeSummary | None] = [None] * len(providers)

    if not providers:
        return []

    max_workers = max(1, len(providers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_slot: dict[concurrent.futures.Future[ProbeSummary], tuple[int, Provider]] = {}
        for index, provider in enumerate(providers):
            future = executor.submit(
                probe_provider, provider, attempts, timeout_s, insecure, ca_file, deadline
            )
            future_to_slot[future] = (index, provider)

        if deadline is None:
            remaining = None
        else:
            remaining = max(0.0, deadline - time.monotonic())
        done, not_done = concurrent.futures.wait(
            future_to_slot.keys(), timeout=remaining
        )

        for future in done:
            slot, provider = future_to_slot[future]
            try:
                ordered_summaries[slot] = future.result()
            except Exception as err:
                ordered_summaries[slot] = make_skipped_summary(
                    provider, attempts, f"Probe error: {type(err).__name__}: {err}"
                )

        for future in not_done:
            slot, provider = future_to_slot[future]
            future.cancel()
            ordered_summaries[slot] = make_skipped_summary(
                provider, attempts, "Skipped: total timeout exceeded"
            )

    return [
        summary
        if summary is not None
        else make_skipped_summary(providers[index], attempts, "Skipped: unknown probe state")
        for index, summary in enumerate(ordered_summaries)
    ]


def rank_summaries(summaries: list[ProbeSummary]) -> list[ProbeSummary]:
    def sort_key(item: ProbeSummary) -> tuple[int, float, float]:
        median_success = (
            item.median_latency_ms if item.median_latency_ms is not None else float("inf")
        )
        median_attempt = (
            item.median_attempt_latency_ms
            if item.median_attempt_latency_ms is not None
            else float("inf")
        )
        return (-item.success_count, median_success, median_attempt)

    return sorted(summaries, key=sort_key)


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def format_env_value(value: str) -> str:
    if SAFE_ENV_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def write_env_file(path: Path, kv: dict[str, str]) -> None:
    path = path.expanduser()
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(kv)
    out_lines: list[str] = []
    key_pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in existing_lines:
        match = key_pattern.match(line)
        if not match:
            out_lines.append(line)
            continue

        key = match.group(1)
        if key in remaining:
            out_lines.append(f"{key}={format_env_value(remaining.pop(key))}")
        else:
            out_lines.append(line)

    for key in ENV_KEYS_TO_SET:
        if key in remaining:
            out_lines.append(f"{key}={format_env_value(remaining.pop(key))}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")


def print_human_results(
    ranked: list[ProbeSummary],
    selected: ProbeSummary | None,
    title: str = "Probe results:",
) -> None:
    print(title)
    for summary in ranked:
        health = "OK" if summary.is_healthy else "FAIL"
        median = (
            f"{summary.median_latency_ms:.1f} ms"
            if summary.median_latency_ms is not None
            else "-"
        )
        attempt_median = (
            f"{summary.median_attempt_latency_ms:.1f} ms"
            if summary.median_attempt_latency_ms is not None
            else "-"
        )
        error = f", last_error={summary.last_error}" if summary.last_error else ""
        print(
            f"- {summary.provider.name}: {health}, success={summary.success_count}/{summary.attempts}, "
            f"median={median}, attempt_median={attempt_median}, "
            f"method={summary.provider.test_method}, model={summary.provider.model}{error}"
        )

    if selected:
        print(
            f"\nSelected: {selected.provider.name} "
            f"({selected.provider.base_url}, probe_model={selected.provider.model}, "
            f"session_model={selected.provider.session_model}, "
            f"key={mask_key(selected.provider.api_key)})"
        )
    else:
        print("\nNo healthy provider found.")


def serialize_summary(item: ProbeSummary) -> dict[str, Any]:
    return {
        "name": item.provider.name,
        "base_url": item.provider.base_url,
        "model": item.provider.model,
        "session_model": item.provider.session_model,
        "cheap_only": item.provider.cheap_only,
        "expensive_only": item.provider.expensive_only,
        "test_method": item.provider.test_method,
        "test_path": resolve_probe_path(item.provider),
        "success_count": item.success_count,
        "attempts": item.attempts,
        "median_latency_ms": item.median_latency_ms,
        "median_attempt_latency_ms": item.median_attempt_latency_ms,
        "attempt_latencies_ms": item.attempt_latencies_ms,
        "last_error": item.last_error,
    }


def print_json_results(
    cheap_ranked: list[ProbeSummary],
    expensive_ranked: list[ProbeSummary] | None,
    selected: ProbeSummary | None,
    selected_source: str | None,
) -> None:
    payload: dict[str, Any] = {
        "selected": selected.provider.name if selected else None,
        "selected_source": selected_source,
        "cheap_providers": [serialize_summary(item) for item in cheap_ranked],
        "expensive_providers": (
            [serialize_summary(item) for item in expensive_ranked]
            if expensive_ranked is not None
            else None
        ),
        "providers": (
            [serialize_summary(item) for item in expensive_ranked]
            if expensive_ranked is not None
            else [serialize_summary(item) for item in cheap_ranked]
        ),
    }
    print(json.dumps(payload, ensure_ascii=True))


def main() -> int:
    args = parse_args()
    if args.attempts <= 0:
        print("--attempts must be > 0", file=sys.stderr)
        return 2
    if args.expensive_attempts <= 0:
        print("--expensive-attempts must be > 0", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 2
    if args.total_timeout <= 0:
        print("--total-timeout must be > 0", file=sys.stderr)
        return 2
    if args.expensive_threshold_ms < 0:
        print("--expensive-threshold-ms must be >= 0", file=sys.stderr)
        return 2

    config_path = Path(args.config).expanduser()
    try:
        providers = load_providers(
            config_path, args.test_path, args.model, args.session_model
        )
    except ConfigError as err:
        print(f"Config error: {err}", file=sys.stderr)
        return 2

    try:
        ca_file = None if args.insecure else resolve_ca_file(args.ca_file)
    except ConfigError as err:
        print(f"TLS config error: {err}", file=sys.stderr)
        return 2

    if ca_file and not args.insecure:
        print(f"Using CA bundle: {ca_file}")

    cheap_providers = [item for item in providers if not item.expensive_only]
    expensive_providers = [item for item in providers if not item.cheap_only]
    if not cheap_providers and not expensive_providers:
        print("Config error: no providers enabled for either cheap or expensive stage.")
        return 2

    deadline = time.monotonic() + args.total_timeout
    cheap_summaries = probe_all_providers_parallel(
        providers=cheap_providers,
        attempts=args.attempts,
        timeout_s=args.timeout,
        insecure=args.insecure,
        ca_file=ca_file,
        deadline=deadline,
    )
    cheap_ranked = rank_summaries(cheap_summaries)
    cheap_selected = next((item for item in cheap_ranked if item.is_healthy), None)

    expensive_ranked: list[ProbeSummary] | None = None
    expensive_selected: ProbeSummary | None = None
    selected = cheap_selected
    selected_source = "cheap" if cheap_selected else None

    should_fallback_probe = (
        not args.no_expensive_probe
        and bool(expensive_providers)
        and should_run_expensive_probe(cheap_ranked, args.expensive_threshold_ms)
    )
    if should_fallback_probe:
        print(
            "Cheap probes are all failed/slow "
            f"(>{args.expensive_threshold_ms:.1f} ms). Running expensive fallback probes..."
        )
        expensive_summaries = probe_all_providers_parallel(
            providers=build_expensive_providers(expensive_providers),
            attempts=args.expensive_attempts,
            timeout_s=args.timeout,
            insecure=args.insecure,
            ca_file=ca_file,
            deadline=deadline,
        )
        expensive_ranked = rank_summaries(expensive_summaries)
        expensive_selected = next((item for item in expensive_ranked if item.is_healthy), None)
        if expensive_selected:
            selected = expensive_selected
            selected_source = "expensive"
        elif selected:
            selected_source = "cheap_fallback"

    print_human_results(cheap_ranked, cheap_selected, title="Cheap probe results:")
    if expensive_ranked is not None:
        print()
        print_human_results(
            expensive_ranked, expensive_selected, title="Expensive probe results:"
        )
        if selected_source == "cheap_fallback":
            print(
                "\nExpensive probe found no healthy provider; "
                "falling back to cheap probe winner."
            )
    if args.json:
        print_json_results(cheap_ranked, expensive_ranked, selected, selected_source)

    if not selected:
        return 1

    selected_env = {
        "GOOGLE_GEMINI_BASE_URL": selected.provider.base_url,
        "GEMINI_API_KEY": selected.provider.api_key,
        "GOOGLE_GEMINI_API_KEY": selected.provider.api_key,
        "GEMINI_MODEL": selected.provider.session_model,
        "GOOGLE_GEMINI_MODEL": selected.provider.session_model,
    }

    env_write_target: str | None = args.write_env
    auto_write = False
    if not env_write_target and not args.print_export and not args.no_auto_write:
        env_write_target = DEFAULT_GEMINI_ENV_FILE
        auto_write = True

    if env_write_target:
        env_path = Path(env_write_target)
        write_env_file(env_path, selected_env)
        if auto_write:
            print(f"Auto-updated Gemini env file: {env_path.expanduser()}")
        else:
            print(f"Updated env file: {env_path.expanduser()}")

    if args.print_export:
        for key in ENV_KEYS_TO_SET:
            print(f"export {key}={shlex.quote(selected_env[key])}")

    if not env_write_target and not args.print_export:
        print(
            "Tip: use --print-export to export into your current shell "
            "or --write-env <path> to write a specific env file."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

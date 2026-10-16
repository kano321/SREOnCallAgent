"""Prometheus demo exporter for the SRE On-Call Agent.

The exporter simulates three services and repeats a short fault cycle.
Each service is unhealthy for a different part of the cycle, which makes
it possible to test Prometheus rules and Alertmanager without a real app.
"""

import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CYCLE_SECONDS = int(os.getenv("DEMO_CYCLE_SECONDS", "360"))
START_TIME = time.time()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def pulse(position: float, start: float, ramp: float, hold: float) -> float:
    """Return a 0..1 pulse with linear ramp-up, hold, and linear fall."""
    if position < start:
        return 0.0

    elapsed = position - start
    if elapsed < ramp:
        return elapsed / ramp

    if elapsed < ramp + hold:
        return 1.0

    fall_elapsed = elapsed - ramp - hold
    if fall_elapsed < ramp:
        return 1.0 - (fall_elapsed / ramp)

    return 0.0


SERVICES = {
    "checkout-service": {
        "profile": lambda t: pulse(t, 20, 20, 60),
        "metrics": {
            "http_request_duration_p99": ("up", 0.3, 8.3),
            "db_pool_active_connections": ("up", 3.0, 10.0),
            "db_query_duration_p99_ms": ("up", 45.0, 30000.0),
            "request_queue_depth": ("up", 12.0, 450.0),
        },
    },
    "auth-service": {
        "profile": lambda t: pulse(t, 140, 20, 60),
        "metrics": {
            "pod_memory_usage_percent": ("up", 22.0, 97.0),
            "session_cache_entries": ("up", 500.0, 52000.0),
            "gc_pause_ms": ("up", 5.0, 2100.0),
            "auth_requests_per_sec": ("wave", 320.0, 520.0),
        },
    },
    "payment-service": {
        "profile": lambda t: pulse(t, 260, 20, 60),
        "metrics": {
            "payment_success_rate": ("down", 99.5, 23.0),
            "stripe_api_calls_per_min": ("up", 45.0, 1400.0),
            "stripe_api_error_rate": ("up", 0.5, 95.0),
            "failed_payments_count": ("up", 0.0, 520.0),
        },
    },
}

INTEGER_METRICS = {
    "db_pool_active_connections",
    "request_queue_depth",
    "session_cache_entries",
    "stripe_api_calls_per_min",
    "failed_payments_count",
}


def metric_value(mode: str, baseline: float, peak: float, progress: float, noise: float) -> float:
    if mode == "down":
        value = peak - (peak - baseline) * progress
    elif mode == "wave":
        value = baseline + (peak - baseline) * progress * (1.0 - 0.5 * progress)
    else:
        value = baseline + (peak - baseline) * progress

    return value + random.uniform(-noise, noise)


def render_metrics() -> bytes:
    cycle_position = (time.time() - START_TIME) % CYCLE_SECONDS
    lines: list[str] = []

    for service, config in SERVICES.items():
        progress = config["profile"](cycle_position)
        for metric_name, (mode, baseline, peak) in config["metrics"].items():
            noise = max((peak - baseline) * 0.01, 0.2)
            value = metric_value(mode, baseline, peak, progress, noise)
            rendered = f"{value:.0f}" if metric_name in INTEGER_METRICS else f"{value:.2f}"

            lines.append(f"# HELP {metric_name} simulated metric for {service}")
            lines.append(f"# TYPE {metric_name} gauge")
            lines.append(f'{metric_name}{{service="{service}"}} {rendered}')

    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/metrics":
            body = render_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, message_format: str, *args: object) -> None:
        print(f"[demo-exporter] {message_format % args}", flush=True)


if __name__ == "__main__":
    print(
        f"demo-exporter listening on :8001 (cycle={CYCLE_SECONDS}s)",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()

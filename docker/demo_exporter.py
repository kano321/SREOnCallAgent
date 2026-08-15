"""Demo SRE exporter: simulates three services with repeating incident cycles.

Exposes Prometheus text-format metrics on /metrics for:

  checkout-service  (INC-001 db_pool_exhaustion)   DB pool saturation
  auth-service      (INC-002 memory_leak_oom)      session cache memory leak
  payment-service   (INC-003 external_api_ratelimit) Stripe 429 retry storm

Each service runs a 15-minute incident cycle (baseline -> degradation ->
saturation), phase-offset so the three never saturate simultaneously.
A 30-minute query window always covers a complete curve.

No third-party dependencies: stdlib http.server only.
"""
import time
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

CYCLE_SECONDS = 15 * 60  # full incident cycle per service
START = time.time()


def _clamp01(x):
    return max(0.0, min(1.0, x))


# profile: given cycle position t in [0, CYCLE), return fault progress p in [0,1]
def profile_checkout(t):
    return _clamp01((t - 300) / 300)  # 5min baseline -> 5min ramp -> 5min saturated


def profile_auth(t):
    return t / CYCLE_SECONDS  # slow continuous leak until the "OOM restart"


def profile_payment(t):
    return _clamp01((t - 450) / 225)  # 7.5min baseline -> 3.75min ramp -> saturated


# metric spec: (mode, baseline, peak); mode = up / down / wave
SERVICES = {
    "checkout-service": {
        "phase_offset": 0,
        "profile": profile_checkout,
        "metrics": {
            "http_request_duration_p99":   ("up", 0.3, 8.3),
            "db_pool_active_connections":  ("up", 3, 10),
            "db_query_duration_p99_ms":    ("up", 45, 30000),
            "request_queue_depth":         ("up", 12, 450),
        },
    },
    "auth-service": {
        "phase_offset": 300,
        "profile": profile_auth,
        "metrics": {
            "pod_memory_usage_percent":    ("up", 22, 97),
            "session_cache_entries":       ("up", 500, 52000),
            "gc_pause_ms":                 ("up", 5, 2100),
            "auth_requests_per_sec":       ("wave", 320, 520),
        },
    },
    "payment-service": {
        "phase_offset": 600,
        "profile": profile_payment,
        "metrics": {
            "payment_success_rate":        ("down", 99.5, 23),
            "stripe_api_calls_per_min":    ("up", 45, 1400),
            "stripe_api_error_rate":       ("up", 0.5, 95),
            "failed_payments_count":       ("up", 0, 520),
        },
    },
}


def service_progress(service_cfg) -> float:
    t = (time.time() - START + service_cfg["phase_offset"]) % CYCLE_SECONDS
    return service_cfg["profile"](t)


def metric_value(mode, baseline, peak, p, noise):
    jitter = (random.random() - 0.5) * 2 * noise
    if mode == "down":
        raw = peak - (peak - baseline) * p
    elif mode == "wave":
        raw = baseline + (peak - baseline) * p * (1 - 0.5 * p)
    else:  # up
        raw = baseline + (peak - baseline) * p
    return raw + jitter


def render_metrics() -> bytes:
    lines = []
    for service, cfg in SERVICES.items():
        p = service_progress(cfg)
        for name, (mode, baseline, peak) in cfg["metrics"].items():
            noise = max((peak - baseline) * 0.01, 0.3)
            value = metric_value(mode, baseline, peak, p, noise)
            if name in ("db_pool_active_connections", "request_queue_depth",
                        "failed_payments_count", "session_cache_entries",
                        "stripe_api_calls_per_min"):
                rendered = f"{value:.0f}"
            else:
                rendered = f"{value:.2f}"
            lines.append(f"# HELP {name} simulated metric for {service}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{{service=\"{service}\"}} {rendered}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[demo-exporter]", fmt % args)


if __name__ == "__main__":
    print("demo-exporter listening on :8001 (3 services, cycle=15min)")
    HTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
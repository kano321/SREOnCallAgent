"""Application metrics exposed to Prometheus."""

from prometheus_client import Counter, Gauge, Histogram


HTTP_REQUESTS = Counter(
    "sre_http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "path", "status"),
)

HTTP_REQUEST_DURATION = Histogram(
    "sre_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "path"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "sre_http_requests_in_progress",
    "HTTP requests currently being processed.",
)

ALERTS_RECEIVED = Counter(
    "sre_alerts_received_total",
    "Alertmanager alerts grouped by processing result.",
    ("result",),
)

INVESTIGATIONS = Counter(
    "sre_investigations_total",
    "Incident investigations grouped by trigger and result.",
    ("trigger", "status"),
)

INVESTIGATION_DURATION = Histogram(
    "sre_investigation_duration_seconds",
    "End-to-end incident investigation duration.",
    ("trigger",),
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

INVESTIGATIONS_IN_PROGRESS = Gauge(
    "sre_investigations_in_progress",
    "Incident investigations currently running.",
)

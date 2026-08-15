"""
MetricQueryTool — 指标查询工具（P0 第①步：接入 Prometheus）。

行为：
1. 优先查 Prometheus（环境变量 PROMETHEUS_URL，默认 http://localhost:9090）：
   - 输入应为「指标名关键词」（如 db_pool / p99 / queue），兼容 LLM 偶尔写出的
     PromQL 风格输入（自动去掉 [30m] 范围选择器、单引号转双引号）
   - 关键字匹配指标名（/api/v1/label/__name__/values）→ 逐个范围查询
     （/api/v1/query_range；窗口 = 告警时间前 30 分钟，实时告警场景下
     窗口终点取 alert.timestamp，否则取当前时间；步长 60s）
   - 输出格式与旧模拟版一致（Agent 接口零改动）
2. 错误分级（E2E 实测后修正）：
   - 连接失败（URLError / 超时）→ 降级到剧本模拟数据，明确标注 [SIMULATED]
   - 查询无效（HTTP 400 / status=error）→ 返回清晰错误提示，绝不降级，
     避免幻觉证据混入（让 Agent 换关键词重试）

注：窗口锚点规则见 _window()——剧本事故用当前时间，webhook 实时告警用告警时间戳
（即规划中的「告警时间前 30 分钟窗口」）。
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_PROMETHEUS_URL = "http://localhost:9090"
WINDOW_SECONDS = 30 * 60  # 告警前 30 分钟窗口（P0① 先用「当前时间前 30 分钟」）
STEP_SECONDS = 60
MAX_AVAILABLE_NAMES = 15

_RANGE_SELECTOR_RE = re.compile(r"\[[^\]]*\]")
_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


class MetricQueryTool:
    """
    Queries time-series metrics for the incident's service.

    Backend: Prometheus (live). Falls back to the incident's simulated
    metrics when Prometheus is unreachable.

    Input to run(): a metric name keyword (e.g. 'db_pool', 'latency', 'memory')
    Returns: time-series data as a formatted string
    """

    name = "metric_query"
    description = (
        "Query time-series metrics for the incident service from Prometheus. "
        "Input: a SIMPLE metric name keyword (e.g. 'db_pool', 'p99', 'queue', 'memory'). "
        "Do NOT write PromQL expressions, labels or range selectors like [30m]. "
        "Returns time-series values showing how the metric changed over the last 30 minutes."
    )

    def __init__(self, incident_data: Dict[str, Any]):
        self.metrics: Dict[str, Dict] = incident_data.get("metrics", {})
        self.service: str = incident_data.get("service", "unknown")
        self.prometheus_url: str = os.getenv(
            "PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL
        ).rstrip("/")
        self.timeout: float = float(os.getenv("PROMETHEUS_TIMEOUT", "5"))

    def run(self, metric_name: str) -> str:
        query = metric_name.strip()
        if not query:
            return self._no_match_message(metric_name, [])

        try:
            return self._query_prometheus(query)
        except HTTPError as exc:
            # 查询被 Prometheus 拒绝（400 等）：是查询本身的问题，不降级，
            # 明确告诉 Agent 换一个简单关键词。
            detail = self._extract_prometheus_error(exc)
            return (
                f"Prometheus rejected the query '{metric_name}'"
                + (f" ({detail})" if detail else "")
                + ".\n"
                "metric_query expects a SIMPLE metric name keyword "
                "(e.g. 'db_pool', 'p99', 'queue'). "
                f"Available metrics: {self._available_names()}"
            )
        except (ValueError, KeyError) as exc:
            # Prometheus 返回 status=error（查询语法问题），同上处理
            return (
                f"Prometheus rejected the query '{metric_name}' ({exc}).\n"
                "metric_query expects a SIMPLE metric name keyword "
                "(e.g. 'db_pool', 'p99', 'queue'). "
                f"Available metrics: {self._available_names()}"
            )
        except (URLError, TimeoutError, OSError) as exc:
            # 连接层失败：Prometheus 真的不可达 → 降级模拟数据
            return self._fallback_simulated(metric_name, exc)

    # ------------------------------------------------------------------
    # Prometheus path
    # ------------------------------------------------------------------
    def _query_prometheus(self, raw_query: str) -> str:
        expr = self._normalize_promql(raw_query)
        base = self._base_name(expr)
        names = self._api_get("/api/v1/label/__name__/values")
        matched = sorted(n for n in names if base.lower() in n.lower())

        if matched:
            parts = [
                f"Metrics for {self.service} matching '{base}' (Prometheus, UTC):"
            ]
            for name in matched:
                series = self._range_query(name)
                if series:
                    parts.append(self._format_series(name, series))
            if len(parts) > 1:
                return "\n".join(parts)

        # 关键字没匹配上 → 把规范化后的输入当 PromQL 表达式直接试一次
        series = self._range_query(expr)
        if series:
            return "\n".join(
                [
                    f"Metrics for {self.service} (Prometheus, UTC):",
                    self._format_series(expr, series),
                ]
            )
        return self._no_match_message(raw_query, names)

    @staticmethod
    def _normalize_promql(expr: str) -> str:
        """容忍 LLM 的 PromQL 习惯：去掉 [30m] 范围选择器，单引号转双引号。"""
        expr = _RANGE_SELECTOR_RE.sub("", expr).strip()
        return expr.replace("'", '"')

    @staticmethod
    def _base_name(expr: str) -> str:
        """提取表达式开头的指标名（如 http_request_duration_p99）。"""
        m = _NAME_RE.match(expr)
        return m.group(0) if m else expr

    def _api_get(self, path: str, params: Dict[str, Any] = None) -> Any:
        url = self.prometheus_url + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") != "success":
            raise ValueError(payload.get("error", "unknown Prometheus error"))
        return payload.get("data")

    def _window(self) -> tuple:
        """
        查询窗口：默认「当前时间前 30 分钟」。
        P0② 起支持真实告警：若 incident 自带 alert.timestamp 且落在最近 2 小时内
        （实时告警场景），则窗口终点取告警时间，即「告警时间前 30 分钟」。
        剧本事故（如 2024 年的历史时间戳）超出范围 → 仍用当前时间。
        """
        end = int(time.time())
        ts = (self.incident_data or {}).get("alert", {}).get("timestamp")
        if ts:
            try:
                alert_ts = int(
                    datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                )
            except (ValueError, TypeError):
                alert_ts = None
            if alert_ts is not None and end - 2 * 3600 <= alert_ts <= end + 300:
                end = alert_ts
        return end - WINDOW_SECONDS, end

    def _range_query(self, promql: str) -> List[Dict]:
        """窗口范围查询，返回 result 系列列表。"""
        start, end = self._window()
        data = self._api_get(
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": STEP_SECONDS},
        )
        return data.get("result", []) if data else []

    def _format_series(self, name: str, series: List[Dict]) -> str:
        lines = []
        for s in series:
            labels = s.get("metric", {})
            extra = {
                k: v
                for k, v in labels.items()
                if k not in ("__name__", "job", "instance")
            }
            label_str = ""
            if extra:
                label_str = (
                    "{" + ", ".join(f"{k}={v}" for k, v in sorted(extra.items())) + "}"
                )
            points = []
            for ts, val in s.get("values", []):
                hhmm = datetime.fromtimestamp(
                    float(ts), tz=timezone.utc
                ).strftime("%H:%M")
                points.append(f"{hhmm}: {val}")
            lines.append(f"  {name}{label_str}: [{ ' | '.join(points) }]")
        return "\n".join(lines)

    def _available_names(self) -> str:
        try:
            names = self._api_get("/api/v1/label/__name__/values")
            return ", ".join(sorted(names)[:MAX_AVAILABLE_NAMES]) or "none"
        except Exception:
            return ", ".join(sorted(self.metrics.keys())) or "none"

    def _no_match_message(self, query: str, names: List[str]) -> str:
        available = ", ".join(names[:MAX_AVAILABLE_NAMES]) if names else "none"
        if len(names) > MAX_AVAILABLE_NAMES:
            available += f" ... ({len(names)} total)"
        return (
            f"No metrics found matching '{query}' for {self.service}.\n"
            f"Available metrics: {available}\n"
            "Hint: use a short keyword (e.g. 'db_pool', 'p99', 'queue')."
        )

    @staticmethod
    def _extract_prometheus_error(exc: HTTPError) -> str:
        """从 Prometheus 错误响应体里提取 error 字段。"""
        try:
            body = exc.read().decode("utf-8", "replace")
            payload = json.loads(body)
            return payload.get("error", "") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Simulated fallback path
    # ------------------------------------------------------------------
    def _fallback_simulated(self, metric_name: str, exc: Exception) -> str:
        query = metric_name.lower().strip()
        matched = {k: v for k, v in self.metrics.items() if query in k.lower()}

        header = f"[SIMULATED] Prometheus 不可达（{exc}），已回退剧本模拟数据。"
        if not matched:
            available = ", ".join(self.metrics.keys())
            return (
                f"{header}\n"
                f"No metrics found matching '{metric_name}' for {self.service}.\n"
                f"Available metrics: {available}"
            )

        lines = [header, f"Metrics for {self.service} matching '{metric_name}':"]
        for name, values in matched.items():
            series = " | ".join(f"{t}: {v}" for t, v in sorted(values.items()))
            lines.append(f"  {name}: [{series}]")
        return "\n".join(lines)
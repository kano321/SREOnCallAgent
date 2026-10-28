# SRE On-Call Agent 运维 Runbook

## 告警与面板对应关系

| 告警名称 | 主要信号 | 首先查看的面板 |
|---|---|---|
| `SreOnCallAgentDown` | `up{job="sre-oncall-agent"} == 0` | Grafana 应用健康 |
| `SreOnCallAgentHigh5xxRate` | 5xx 响应比例超过 5% | 请求速率和 5xx 比例 |
| `SreOnCallAgentHighLatency` | P95 HTTP 延迟超过 1 秒 | HTTP 延迟 |
| `SreInvestigationFailures` | 最近五分钟存在调查失败 | 调查吞吐和失败状态 |
| `DBConnectionPoolSaturation` | 模拟结账服务连接池饱和 | 模拟业务指标 |
| `PodMemoryHigh` | 模拟认证服务内存过高 | 模拟业务指标 |
| `PaymentSuccessRateLow` | 模拟支付服务 SLO 下降 | 模拟业务指标 |

## 故障处理顺序

1. 确认告警是否仍在触发，以及是否有真实用户影响。
2. 检查最近的发布、配置变更和数据库变更。
3. 验证应用存活、就绪和 Prometheus 抓取状态。
4. 检查 HTTP 错误、延迟、调查失败和依赖健康状态。
5. 选择影响最小的止损措施，同时保留日志和事件证据。

删除 Pod 或重启数据库前，应先保存日志、事件和 Pod 描述，避免破坏现场。

## 应用不可用

现象：

- `SreOnCallAgentDown` 触发。
- Prometheus 无法抓取 `/metrics`。
- Grafana 停止更新应用指标。

排查命令：

```bash
kubectl -n sre-oncall get pods -o wide
kubectl -n sre-oncall describe pod -l app.kubernetes.io/name=sre-oncall-agent
kubectl -n sre-oncall logs deployment/sre-oncall-agent --tail=200
kubectl -n sre-oncall get events --sort-by=.lastTimestamp
```

检查项：

1. Pod 是 `Pending`、`ImagePullBackOff`、`CrashLoopBackOff` 还是 `Running`。
2. 是否因 CPU、内存限制发生驱逐或 OOMKilled。
3. ConfigMap 和 Secret 是否存在，键名是否正确。
4. 目标节点是否已经加载镜像。
5. 最近一次发布是否引入了错误配置。

回滚命令：

```bash
kubectl -n sre-oncall rollout undo deployment/sre-oncall-agent
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
```

## 就绪检查失败

现象：

- Pod 处于 Running，但 READY 为 `0/1`。
- `/ready` 返回 HTTP 503。
- 日志中出现 MySQL 连接或认证错误。

排查命令：

```bash
kubectl -n sre-oncall exec deploy/sre-oncall-agent -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read())"

kubectl -n sre-oncall get pods -l app.kubernetes.io/name=sre-mysql
kubectl -n sre-oncall logs statefulset/sre-mysql --tail=200
kubectl -n sre-oncall get pvc
kubectl -n sre-oncall describe pvc mysql-data-sre-mysql-0
```

重点检查：

- `MYSQL_USER`、`MYSQL_PASSWORD` 和 `MYSQL_DB` 是否正确。
- PVC 是否处于 `Bound` 状态。
- MySQL Pod 是否 Ready。
- Service 名称 `sre-mysql` 是否能正常解析。

## 5xx 比例升高

查询错误请求：

```promql
sum by (path, status) (
  rate(sre_http_requests_total{job="sre-oncall-agent",status=~"5.."}[5m])
)
```

查看日志：

```bash
kubectl -n sre-oncall logs deployment/sre-oncall-agent --tail=300
```

常见原因：

- MySQL 不可用时报告持久化失败。
- LLM 配置错误或上游供应商故障。
- Webhook 数据格式错误。
- 认证数据库查询失败。

如果只是报告持久化失败，当前进程内可能仍保留调查结果。应先确认 `persistence_warning`，修复 MySQL 后再决定是否重启。

## 延迟升高

查询 P95 延迟：

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(sre_http_request_duration_seconds_bucket{job="sre-oncall-agent"}[5m])
  )
)
```

事故调查会调用外部模型，天然比健康检查慢。分析时应区分：

- `/health` 和 `/ready`。
- 普通 API 请求。
- `/incidents/investigate` 和 SSE 长连接。

重点检查：

- LLM 上游延迟和限流。
- MySQL 查询延迟与连接数。
- CPU 限流和内存压力。
- 慢客户端是否长期占用 SSE 连接。

## 调查失败

查看日志：

```bash
kubectl -n sre-oncall logs deployment/sre-oncall-agent --tail=500
```

查询失败指标：

```promql
increase(sre_investigations_total{job="sre-oncall-agent",status="error"}[15m])
```

检查：

1. `LLM_API_KEY` 是否有效。
2. Pod 是否可以访问 `LLM_BASE_URL`。
3. `LLM_MODEL_ID` 是否对当前账号开放。
4. 故障样例是否存在。
5. 模型返回是否为空或无法解析。

排查时不要打印 API Key。可以检查 Secret 是否存在、环境变量是否注入和网络是否可达，但不要输出密钥内容。

## 重复告警或告警丢失

Webhook 返回中的 `started` 表示已启动调查，`skipped` 表示被去重。

发送测试请求：

```bash
curl -i -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: $WEBHOOK_TOKEN" \
  --data @alert.json
```

如果所有请求都被跳过，应比较指纹和 `startsAt`。去重记录会在 `WEBHOOK_DEDUP_SECONDS` 后过期。

## 恢复确认

```bash
kubectl -n sre-oncall get pods
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/metrics
```

恢复标准：

- 应用和 MySQL 都为 Ready。
- Prometheus 目标 `sre-oncall-agent` 为 `up`。
- Grafana 能看到新的请求指标。
- 手动事故调查可以完成。
- 报告可以写入 MySQL 并在历史报告中查看。

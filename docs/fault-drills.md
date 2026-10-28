# 故障演练手册

以下演练适用于本地 Docker Compose 或 Kind 环境。建议每次记录时间、现象、证据、根因、止损措施和防复发行动。

## 演练一：应用 Pod 自愈

注入故障：

```bash
kubectl -n sre-oncall delete pod -l app.kubernetes.io/name=sre-oncall-agent
```

观察恢复：

```bash
kubectl -n sre-oncall get pods -w
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
```

预期结果：

- 旧 Pod 被终止。
- ReplicaSet 自动创建新 Pod。
- 启动探针和就绪探针通过。
- Service 无需人工干预即可恢复访问。

面试讲解重点：说明存活探针、就绪探针和启动探针的区别。

## 演练二：滚动重启

执行：

```bash
kubectl -n sre-oncall rollout restart deployment/sre-oncall-agent
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
```

预期结果：

- 创建新的 ReplicaSet。
- 单副本部署会短暂没有可用应用实例。
- 新 Pod 完成数据库连接和初始化后进入 Ready。

面试讲解重点：解释为什么在进程内保存状态时使用 `maxSurge: 0` 和 `maxUnavailable: 1`，以及多副本前必须先迁移共享状态。

## 演练三：数据库依赖故障

停掉 MySQL：

```bash
docker compose stop mysql
curl -i http://localhost:8000/ready
```

预期结果：

- `/health` 仍表示进程存活。
- `/ready` 返回 HTTP 503。
- 应用日志出现 MySQL 连接错误。

恢复：

```bash
docker compose start mysql
```

恢复标准：

- MySQL 变为 healthy。
- `/ready` 在连接恢复后返回 HTTP 200。
- 数据卷中的历史数据仍然存在。

面试讲解重点：存活检查不能依赖数据库，否则数据库故障会导致应用被反复重启；依赖可用性应通过就绪检查表达。

## 演练四：Prometheus 抓取失败

在 Kubernetes 中将应用缩容到零：

```bash
kubectl -n sre-oncall scale deployment/sre-oncall-agent --replicas=0
```

查询指标：

```promql
up{job="sre-oncall-agent"}
```

预期结果：

- 应用目标变为 `down`。
- `SreOnCallAgentDown` 先进入 Pending，再进入 Firing。
- Alertmanager 收到告警。

恢复并确认：

```bash
kubectl -n sre-oncall scale deployment/sre-oncall-agent --replicas=1
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
```

面试讲解重点：解释抓取间隔、`for`、Pending、Firing 和 Resolved 的生命周期。

## 演练五：重复 Webhook

使用 `.env` 中配置的 Token，将同一个 Alertmanager 请求发送两次。

预期结果：

- 第一次请求在 `started` 中返回一个调查。
- 第二次请求被标记为重复。
- `sre_alerts_received_total{result="duplicate"}` 增加。

面试讲解重点：说明告警指纹、去重时间窗口，以及多副本场景为什么需要 Redis 或数据库。

## 演练六：LLM 供应商故障

在测试环境使用临时无效 Key，或者阻断 Pod 到模型供应商的网络。

预期结果：

- API 本身仍保持健康。
- 调查返回模型配置或网络错误。
- `sre_investigations_total{status="error"}` 增加。

恢复有效配置后重新执行调查。

面试讲解重点：解释超时、重试、降级，以及外部模型故障为什么不应该导致整个控制面不可用。

## 故障记录模板

```text
事故名称：
开始时间：
发现方式：
用户影响：
最近变更：
监控指标：
日志证据：
根因：
止损措施：
恢复确认：
防复发行动：
```

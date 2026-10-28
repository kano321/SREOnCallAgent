# 面试演示指南

## 90 秒中文自我介绍

> 我是江苏大学物联网工程专业 2027 届本科生。我的经历包括运维开发和 IT 运维两部分：做过 OA 数据修复、Redis 分布式锁、定时任务、接口故障排查和安全事件闭环。个人项目方面，我实现了一套 AI SRE 值班平台，能够接收 Alertmanager 告警，调用日志、指标和 Runbook 工具进行调查，生成 RCA 复盘报告，并通过 SSE 实时展示进度。这个项目还完成了 MySQL 持久化、Prometheus 指标、Grafana 面板、非 root 容器化，以及在三节点 Kubernetes 集群中的部署和故障演练。我的主要优势是 Python 后端、Linux 排障和可观测性，目前重点补强 Kubernetes 网络和 Go。

## 五分钟演示流程

### 第一部分：说明架构

打开 [架构说明](architecture.md)，用一句话说明：

`Alertmanager → FastAPI → 三阶段 Agent → 证据 → RCA → MySQL`。

重点说明这是事故调查控制面，不负责运行业务负载。

### 第二部分：健康检查与监控

打开以下地址：

- `http://localhost:8000/health`
- `http://localhost:8000/ready`
- `http://localhost:3000/d/sre-oncall-agent`

解释：

- `/health` 判断进程是否存活。
- `/ready` 判断 MySQL 是否可用。
- Grafana 展示请求速率、延迟、5xx 比例、正在执行的调查和告警处理结果。

### 第三部分：故障演练

执行其中一条：

```bash
kubectl -n sre-oncall delete pod -l app.kubernetes.io/name=sre-oncall-agent
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
```

然后展示 Pod 日志、事件和恢复后的 READY 状态。

### 第四部分：事故调查

从页面或 API 启动一个故障样例，讲解：

- 分诊阶段生成调查计划。
- ReAct 阶段按计划调用工具。
- 日志和指标证据写入 findings。
- Reflection 阶段评审并修订报告。
- SSE 让浏览器实时看到进度，不需要轮询。

### 第五部分：主动说明限制

最后说明当前边界：

- 去重和 SSE 分发状态在进程内，所以 Kubernetes 只运行一个应用副本。
- 下一步会使用 Redis 迁移共享状态。
- MySQL StatefulSet 是演示配置，不是生产级高可用数据库。
- 当前故障日志和指标来自样例，真实环境需要接入 Loki 和 Kubernetes 指标。

## 高频面试问题

### 为什么使用 SSE，不使用 WebSocket？

服务端只需要单向推送事件，不需要双向通信。SSE 使用普通 HTTP，部署和运维更简单，FastAPI 可以直接返回 `StreamingResponse`。

代价是 EventSource 不能设置自定义请求头，因此本项目额外支持通过查询参数传递 Bearer Token。

### 为什么 Kubernetes 只部署一个副本？

告警去重、SSE 订阅者和会话报告保存在进程内存中。多个副本可能重复启动调查，也无法共享事件流。

正确处理不是强行增加副本数，而是先把这些状态迁移到 Redis，并补充跨副本测试。

### 告警幂等是怎么做的？

去重键由 Alertmanager 指纹和 `startsAt` 组成。正在执行或最近出现过的键会被跳过，记录在 `WEBHOOK_DEDUP_SECONDS` 后过期。

### MySQL 不可用会发生什么？

进程仍然存活，`/ready` 返回 503。数据库相关操作失败，但已经完成的调查结果会保留在当前进程内，并通过 `persistence_warning` 通知前端。

### 测试如何避免真实调用大模型？

测试会 Mock 大模型和 Agent 类，覆盖接口认证、告警转换、去重、数据库重试、工具调用和流水线编排。测试不需要真实 MySQL、LLM 或外网。

### 如何把项目扩展到多副本？

- 将告警去重和运行状态迁移到 Redis。
- 使用 Redis Pub/Sub 或消息队列做 SSE 广播。
- 将报告统一写入 MySQL 或对象存储。
- 给长调查任务增加独立工作队列。
- 增加 HPA、PodDisruptionBudget 和 MySQL 高可用。

### Pod 一直不 Ready 怎么排查？

通过 `kubectl get`、`describe`、`logs` 和 `get events` 查看启动探针、就绪探针和容器状态，再检查 MySQL、Secret、ConfigMap、资源限制和镜像状态。

### 下一步最值得做什么？

最优先的是 Redis 共享状态，让系统具备多副本运行条件。其次是接入 Loki 和真实 Kubernetes 指标，最后补充审计日志、API 限流和安全的 Runbook 自动执行。

## 简历描述建议

只写可以现场演示的内容，例如：

- 构建容器化 AI SRE 调查平台，实现三阶段 Agent 编排、SSE 实时推送和 MySQL 报告持久化。
- 增加 Prometheus 业务指标、4 条应用告警和 7 面板 Grafana 监控台。
- 使用 Kustomize 部署三节点 Kind 集群，配置探针、资源限制、MySQL PVC、非 root 和只读根文件系统。
- 增加 38 个 pytest 测试，覆盖率约 71%，并通过 GitHub Actions 检查测试、Compose、Kubernetes 清单和镜像构建。

在 Redis 和 MySQL 高可用完成之前，不要写“支持生产级多副本”或“生产级高可用”。

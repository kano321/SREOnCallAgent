# SRE On-Call Agent 功能验证测试文档

## 一、文档用途

本文档用于验证本轮可靠性与安全优化是否真正生效，覆盖：

- MySQL 连接池。
- Token 哈希存储与旧数据迁移。
- HttpOnly Cookie 与 SSE 鉴权。
- 历史报告用户隔离。
- 历史报告分页与前端“加载更多”。
- LLM 重试、退避、总时间预算和错误透传。
- Docker、Prometheus、Grafana 和 Kubernetes 运行状态。

本文档区分：

- **必测项**：不依赖额外费用，应当全部通过。
- **可选联调项**：需要真实 LLM Key，会产生少量模型调用。
- **Kubernetes 项**：需要 Docker、kubectl 和 kind。

## 二、开发侧自测结论

截至 2026-10-29，开发侧已完成以下验证：

| 验证项 | 结果 |
|---|---|
| Python 单元与接口测试 | 53 个测试全部通过 |
| 测试覆盖率 | 77.40%，CI 门槛 70% |
| Python 3.12 容器测试 | 通过 |
| Docker Compose 服务 | 6 个服务健康 |
| MySQL 连接池 | 返回 `PooledDedicatedDBConnection` 和字典游标 |
| Token 迁移 | 数据库中 `is_hashed=0` 的数量为 0 |
| Cookie 鉴权 | 登录后可仅凭 Cookie 调用受保护接口 |
| 无 URL Token SSE | 使用 Cookie 访问 SSE 鉴权入口返回预期的 404，而不是 401 |
| 报告分页 | 首屏 20 条，点击“加载更多”后加载剩余 8 条 |
| 报告隔离 | 用户 A 看不到用户 B 的个人报告 |
| Prometheus | 2/2 抓取目标正常，7 条告警规则加载 |
| Grafana | 面板正常加载，共 7 个 Panel |
| Kubernetes | 三节点 Kind 部署、PVC、Cookie、自愈和只读根文件系统通过 |
| 浏览器 | 登录、分页、系统报告展示正常，无 JavaScript 错误 |

这份结果只能证明开发环境通过。以下步骤用于你独立验收。

## 三、测试前准备

### 3.1 备份现有数据

本轮会修改数据库字段并迁移 Token。正式测试前建议备份 MySQL：

```powershell
docker exec sre-oncall-monitoring-mysql-1 sh -c `
  'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" sre_agent > /tmp/sre_agent_backup.sql'

docker cp sre-oncall-monitoring-mysql-1:/tmp/sre_agent_backup.sql .\sre_agent_backup.sql
```

### 3.2 重新构建应用

代码修改不会因为 `docker compose restart` 自动加载。必须重新构建：

```powershell
docker compose up -d --build
docker compose ps
```

预期：

- `app` 为 healthy。
- `mysql` 为 healthy。
- `prometheus` 和 `alertmanager` 为 healthy。
- `grafana` 和 `demo-exporter` 正常运行。

### 3.3 查看启动日志

```powershell
docker compose logs --tail=200 app
```

不应出现：

- `PooledDBError`
- `TypeError: tuple indices`
- Cookie 配置错误
- 数据库迁移错误

### 3.4 基础健康检查

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

预期：

```json
{"status":"ok"}
{"status":"ready"}
```

## 四、自动化测试

### TC-AUTO-001：全量测试与覆盖率

执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest `
  --cov=src `
  --cov-report=term-missing `
  --cov-fail-under=70
```

预期：

- 53 个测试通过。
- 覆盖率不低于 70%。
- 没有测试直接访问真实 LLM 或真实 MySQL。

通过标准：

```text
53 passed
Required test coverage of 70% reached
```

### TC-AUTO-002：LLM 重试专项测试

执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_agents.py -v
```

重点确认以下测试通过：

- `test_llm_retries_rate_limit_and_returns_content`
- `test_llm_does_not_retry_auth_error`
- `test_llm_retries_empty_response`
- `test_llm_budget_exhaustion_is_explicit`

预期：

- 429、5xx、网络错误和空响应可重试。
- 401、403、模型不存在等错误不会重试。
- 总时间预算耗尽时返回 `budget_exhausted`。

### TC-AUTO-003：配置与清单校验

```powershell
docker compose config --quiet
kubectl kustomize deploy/k8s | Out-Null
node --check static/app.js
node --check static/login.js
```

预期：

- 命令退出码为 0。
- 没有 YAML、Kustomize 或 JavaScript 语法错误。

## 五、数据库与连接池验证

### TC-DB-001：连接池和字典游标

执行：

```powershell
docker exec sre-oncall-monitoring-app-1 python -c `
  "from src.api import db; c1=db.conn(); c2=db.conn(); cur=c1.cursor(); cur.execute('SELECT 1 AS x'); print(type(c1).__name__, type(c2).__name__, cur.fetchone()); c1.close(); c2.close()"
```

预期类似：

```text
PooledDedicatedDBConnection PooledDedicatedDBConnection {'x': 1}
```

通过标准：

- 连接类型是池化连接，不是 `pymysql.connections.Connection`。
- 查询结果是字典，不是元组。

### TC-DB-002：数据库迁移字段

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password `
  -e "USE sre_agent; SHOW COLUMNS FROM tokens; SHOW COLUMNS FROM reports;"
```

预期：

- `tokens` 包含 `is_hashed`。
- `reports` 包含 `owner_username`。
- `reports.owner_username` 的默认值为 `legacy`。

### TC-DB-003：旧 Token 已全部迁移

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password -N -B `
  -e "USE sre_agent; SELECT COUNT(*) FROM tokens WHERE is_hashed=0;"
```

预期：

```text
0
```

## 六、Token 与 Cookie 验证

### TC-SEC-001：新登录 Token 只保存哈希

先通过页面注册并登录一个新测试账号，然后执行：

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password `
  -e "USE sre_agent; SELECT username, is_hashed, LENGTH(token) AS token_length FROM tokens ORDER BY created_at DESC LIMIT 5;"
```

预期：

- 新登录账号的 `is_hashed` 为 1。
- `token_length` 为 64。
- 数据库中不存在原始 Token。

注意：长度不能单独证明是哈希，必须同时检查 `is_hashed=1`。

### TC-SEC-002：Cookie 可以单独认证

在 PowerShell 中注册并登录测试用户：

```powershell
$username = "cookie_test_$(Get-Date -Format 'yyyyMMddHHmmss')"
$password = "test123456"

Invoke-RestMethod -Method Post http://localhost:8000/auth/register `
  -ContentType application/json `
  -Body (@{
    username = $username
    password = $password
    question = "security question"
    answer = "answer123"
  } | ConvertTo-Json)

$login = Invoke-RestMethod -Method Post http://localhost:8000/auth/login `
  -ContentType application/json `
  -Body (@{
    username = $username
    password = $password
  } | ConvertTo-Json) `
  -SessionVariable session

Invoke-RestMethod http://localhost:8000/auth/me -WebSession $session
```

预期：

```json
{"username":"cookie_test_..."}
```

通过标准：

- 第二次调用 `/auth/me` 没有手工添加 Authorization 请求头。
- 认证来自 `sre_session` Cookie。

### TC-SEC-003：SSE 不依赖 URL Token

保持同一个 `$session`，访问一个不存在的故障样例：

```powershell
try {
  Invoke-WebRequest `
    "http://localhost:8000/incidents/investigate/stream?incident_id=does-not-exist" `
    -WebSession $session
} catch {
  [int]$_.Exception.Response.StatusCode
}
```

预期：

```text
404
```

不是 401，说明 Cookie 已通过认证，随后因为故障样例不存在返回 404。

### TC-SEC-004：旧查询参数 Token 默认关闭

```powershell
$rawToken = $login.token
try {
  Invoke-WebRequest "http://localhost:8000/auth/me?token=$rawToken"
} catch {
  [int]$_.Exception.Response.StatusCode
}
```

预期：

```text
401
```

如果返回 200，说明环境显式开启了 `ALLOW_LEGACY_QUERY_TOKEN=true`，不符合默认安全配置。

### TC-SEC-005：浏览器 Cookie 属性

1. 打开 Chrome 开发者工具。
2. 进入 Application 或 Storage。
3. 查看 `http://localhost:8000` 的 Cookie。
4. 找到 `sre_session`。

预期：

- HttpOnly 已勾选。
- SameSite 为 Lax。
- Secure 在本地 HTTP 环境可以为空。
- 生产 HTTPS 环境应设置 `SESSION_COOKIE_SECURE=true`。

## 七、报告隔离与分页验证

### TC-REP-001：不同用户不能看到对方个人报告

创建两个账号 A、B。让用户 A 通过页面完成一次真实调查，然后分别登录 A、B。

调用：

```powershell
Invoke-RestMethod "http://localhost:8000/incidents/reports?limit=50&offset=0" `
  -Headers @{ Authorization = "Bearer $tokenA" }

Invoke-RestMethod "http://localhost:8000/incidents/reports?limit=50&offset=0" `
  -Headers @{ Authorization = "Bearer $tokenB" }
```

预期：

- A 的列表中可以看到 `owner_username` 为 A 的报告。
- B 的列表中不能出现 `owner_username` 为 A 的报告。
- `owner_username` 为 `system` 的自动告警报告可以同时被 A、B 查看。

### TC-REP-002：系统告警报告只读

在页面查看 `owner_username=system` 的报告。

预期：

- 报告显示“系统”标识。
- 页面不显示删除按钮。
- 登录用户可以查看，但只有报告所有者可以删除个人报告。

### TC-REP-003：接口分页字段

```powershell
$page = Invoke-RestMethod `
  "http://localhost:8000/incidents/reports?limit=1&offset=0" `
  -Headers @{ Authorization = "Bearer $tokenA" }

$page.pagination
```

预期包含：

```json
{
  "limit": 1,
  "offset": 0,
  "total": 2,
  "has_more": true
}
```

实际 `total` 取决于数据库数据。

### TC-REP-004：前端“加载更多”

如果历史报告少于 20 条，测试按钮不会出现。可以插入 25 条验收数据：

```powershell
$env:OWNER_USERNAME = "填写你的测试用户名"

@'
import json
import os
from src.api import db

owner = os.environ["OWNER_USERNAME"]
for i in range(1, 26):
    payload = {
        "incident_id": f"acceptance-page-{i}",
        "service": "acceptance-service",
        "severity": "P3",
        "plan": [],
        "findings": {},
        "report": f"分页验收报告 {i}",
    }
    db.execute(
        "INSERT INTO reports "
        "(incident_id, service, severity, owner_username, created_at, payload) "
        "VALUES (%s, %s, %s, %s, NOW(), %s)",
        (
            payload["incident_id"],
            payload["service"],
            payload["severity"],
            owner,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
print("inserted=25")
'@ | docker exec -i -e OWNER_USERNAME=$env:OWNER_USERNAME `
  sre-oncall-monitoring-app-1 python -
```

然后刷新监控台。

预期：

- 首屏最多显示 20 条报告。
- 底部出现“加载更多（20/总数）”。
- 点击后剩余报告追加到列表。
- 全部加载完成后按钮隐藏。

清理验收数据：

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password `
  -e "USE sre_agent; DELETE FROM reports WHERE incident_id LIKE 'acceptance-page-%';"
```

## 八、LLM 错误与重试验证

### TC-LLM-001：自动化重试验证

执行专项测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_agents.py -v
```

预期：

- 429 第一次失败后重试成功。
- 401 不重试。
- 空响应可以重试。
- 总预算耗尽时返回明确错误。

### TC-LLM-002：页面错误透传

在测试环境把 `LLM_BASE_URL` 临时改为不可访问地址：

```dotenv
LLM_BASE_URL=http://127.0.0.1:1/v1
LLM_MAX_RETRIES=1
LLM_REQUEST_BUDGET_SECONDS=10
```

重新创建应用容器：

```powershell
docker compose up -d --force-recreate app
```

执行一次调查。

预期：

- 页面调查卡显示网络错误。
- 不会静默停止。
- API 服务继续健康。
- 恢复正确配置后重新启动应用即可调查。

此测试需要修改应用配置。完成后必须恢复原 `.env`。

## 九、前端与 SSE 验证

### TC-FE-001：登录和监控台

1. 打开 `http://localhost:8000/login.html`。
2. 注册并登录。
3. 检查事故样例和历史报告是否加载。

预期：

- 登录后跳转监控台。
- 页面显示用户名和服务状态。
- 历史报告列表可见。
- 浏览器控制台没有 JavaScript 错误。

### TC-FE-002：SSE URL 不含长期 Token

1. 选择 `db_pool_exhaustion`。
2. 点击“开始调查”。
3. 在开发者工具 Network 中查看 `/incidents/investigate/stream` 请求。
4. 查看 Request URL 和 Cookies。

预期：

- URL 只有 `incident_id`。
- URL 中不存在 `token=`。
- 请求携带 `sre_session` Cookie。
- 页面收到 stage、plan、step、report 和 done 事件。

## 十、监控验证

### TC-MON-001：Prometheus 目标

打开：

```text
http://localhost:9090/targets
```

预期：

- `sre-oncall-agent` 为 UP。
- `demo-exporter` 为 UP。

### TC-MON-002：应用指标

打开：

```text
http://localhost:9090/graph
```

查询：

```promql
sre_http_requests_total
```

```promql
sre_investigations_total
```

```promql
sre_alerts_received_total
```

预期：

- 指标有数据。
- 状态码、来源和告警处理结果标签正常。

### TC-MON-003：Grafana

打开：

```text
http://localhost:3000/d/sre-oncall-agent
```

预期：

- Application Health 显示 UP。
- HTTP Request Rate、5xx Ratio、Latency 有数据。
- Investigation Throughput 和 Active Investigations 正常。
- Alert Processing 面板可以显示结果。

### TC-MON-004：告警规则

打开：

```text
http://localhost:9090/rules
```

预期存在 7 条规则：

- `SreOnCallAgentDown`
- `SreOnCallAgentHigh5xxRate`
- `SreOnCallAgentHighLatency`
- `SreInvestigationFailures`
- `DBConnectionPoolSaturation`
- `PodMemoryHigh`
- `PaymentSuccessRateLow`

## 十一、Kubernetes 验证

完整操作见 [Kubernetes 部署说明](../deploy/k8s/README.md)。

### TC-K8S-001：三节点部署

```powershell
kind create cluster --config deploy/k8s/kind-config.yaml
docker build -t sre-oncall-agent:local .
kind load docker-image sre-oncall-agent:local --name sre-oncall
```

创建 Secret 后：

```powershell
kubectl apply -k deploy/k8s
kubectl -n sre-oncall rollout status statefulset/sre-mysql
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
kubectl get nodes
kubectl -n sre-oncall get pods,pvc
```

预期：

- 三个节点 Ready。
- MySQL 和应用 Pod Ready。
- PVC 为 Bound。
- 应用和 MySQL 可以调度在不同节点。

### TC-K8S-002：Pod 自愈

```powershell
kubectl -n sre-oncall delete pod -l app.kubernetes.io/name=sre-oncall-agent
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent
kubectl -n sre-oncall get pods -o wide
```

预期：

- 新 Pod 自动创建。
- 新 Pod 在 180 秒内 Ready。
- Service 可访问。

### TC-K8S-003：安全上下文

```powershell
kubectl -n sre-oncall exec deployment/sre-oncall-agent -- id
kubectl -n sre-oncall exec deployment/sre-oncall-agent -- `
  sh -c 'touch /forbidden 2>/dev/null; test $? -ne 0 && echo rootfs=read-only'
```

预期：

```text
uid=999(app) gid=999(app)
rootfs=read-only
```

## 十二、验收记录表

| 编号 | 测试项 | 执行结果 | 实际结果或截图编号 | 备注 |
|---|---|---|---|---|
| TC-AUTO-001 | 全量测试 | 未执行 |  |  |
| TC-AUTO-002 | LLM 重试 | 未执行 |  |  |
| TC-AUTO-003 | 配置校验 | 未执行 |  |  |
| TC-DB-001 | 连接池 | 未执行 |  |  |
| TC-DB-002 | 数据库迁移 | 未执行 |  |  |
| TC-DB-003 | Token 迁移 | 未执行 |  |  |
| TC-SEC-001 | Token 哈希 | 未执行 |  |  |
| TC-SEC-002 | Cookie 认证 | 未执行 |  |  |
| TC-SEC-003 | Cookie SSE | 未执行 |  |  |
| TC-SEC-004 | 禁用查询 Token | 未执行 |  |  |
| TC-SEC-005 | Cookie 属性 | 未执行 |  |  |
| TC-REP-001 | 用户隔离 | 未执行 |  |  |
| TC-REP-002 | 系统报告只读 | 未执行 |  |  |
| TC-REP-003 | 分页字段 | 未执行 |  |  |
| TC-REP-004 | 加载更多 | 未执行 |  |  |
| TC-LLM-001 | 自动重试 | 未执行 |  |  |
| TC-LLM-002 | 错误透传 | 未执行 |  | 可选 |
| TC-FE-001 | 登录监控台 | 未执行 |  |  |
| TC-FE-002 | 无 URL Token SSE | 未执行 |  |  |
| TC-MON-001 | Prometheus 目标 | 未执行 |  |  |
| TC-MON-002 | 应用指标 | 未执行 |  |  |
| TC-MON-003 | Grafana | 未执行 |  |  |
| TC-MON-004 | 告警规则 | 未执行 |  |  |
| TC-K8S-001 | 三节点部署 | 未执行 |  | 可选 |
| TC-K8S-002 | Pod 自愈 | 未执行 |  | 可选 |
| TC-K8S-003 | 安全上下文 | 未执行 |  | 可选 |

结果填写建议：

- `通过`
- `失败`
- `阻塞`
- `不适用`

## 十三、失败判定

以下任一情况出现，应判定测试失败：

- 应用或 MySQL 无法进入 Ready。
- 登录接口返回 500。
- 数据库存在 `is_hashed=0` 的新登录 Token。
- SSE 请求 URL 仍包含长期 Token。
- 用户能读取或删除其他用户的个人报告。
- 历史报告分页出现重复、遗漏或 `has_more` 错误。
- 429、5xx、网络错误或空响应没有按配置重试。
- 认证错误、模型不存在错误被错误重试。
- Prometheus 抓取目标为 DOWN。
- Grafana 面板无数据或加载失败。
- Kubernetes Pod 无法自愈。
- 页面出现未处理的 JavaScript 错误。

## 十四、测试后清理

删除分页验收数据：

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password `
  -e "USE sre_agent; DELETE FROM reports WHERE incident_id LIKE 'acceptance-page-%';"
```

删除测试用户：

```powershell
docker exec sre-oncall-monitoring-mysql-1 mysql `
  -usre -psre_dev_password `
  -e "USE sre_agent; DELETE FROM tokens WHERE username LIKE 'cookie_test_%'; DELETE FROM users WHERE username LIKE 'cookie_test_%';"
```

如果执行了 Kubernetes 测试，可以删除临时集群：

```powershell
kind delete cluster --name sre-oncall
```

不要执行 `docker compose down -v`，除非你确定不需要本地 MySQL、Prometheus 和 Grafana 数据。

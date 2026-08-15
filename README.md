# SRE On-Call Agent

> AI 驱动的 SRE 值班助手：告警分诊、根因调查、故障复盘报告自动生成

> **项目简介**：本项目构建了一个 AI 驱动的 SRE 值班助手，自动完成告警分诊、根因调查和故障复盘报告生成。通过三阶段智能体流水线（Plan-and-Solve → ReAct → Reflection）演示了三种经典范式在真实运维场景下的串联应用，是 SRE/运维领域的教学实践项目。

## 📝 项目介绍

凌晨 3 点生产告警响起，值班 SRE 必须顶着压力完成分诊、排查日志与指标、查阅 runbook、撰写复盘报告。本项目用三阶段智能体流水线自动化整个工作流：

- **第一阶段 — TriageAgent（分诊，Plan-and-Solve）**：把原始告警 JSON 转化为有序的调查计划
- **第二阶段 — InvestigationAgent（调查，ReAct）**：循环使用日志搜索、指标查询、runbook 查阅三个工具定位根因
- **第三阶段 — PostmortemAgent（复盘，Reflection）**：起草结构化 RCA 报告，按质量标准自我评审并修订

## ✨ 核心功能

- [x] 三个故障样例：数据库连接池耗尽、内存泄漏 OOM、外部 API 限流级联
- [x] ReAct 调查循环，内置 3 个工具：`log_search`（日志搜索）、`metric_query`（指标查询）、`runbook_lookup`（手册查阅）
- [x] Reflection 复盘报告：草稿 → 评审 → 修订 循环
- [x] FastAPI REST 后端，已开启 CORS
- [x] 结构化 RCA 报告：时间线、5-Why 分析、影响评估、行动项
- [x] **监控台前端**：浏览器实时界面，SSE 流式展示 Agent 每一步推理
- [x] **SSE 实时流**：每个 Thought / Action / Observation 实时推送到浏览器
- [x] **MySQL 报告历史**：所有历史调查持久化存储，可随时浏览
- [x] **用户账号系统**：注册 / 登录 / 登出，Bearer Token 认证（PBKDF2 加盐哈希）
- [x] **密码找回**：通过用户自定义安全问题与答案
- [x] **并发调查**：多个事故并行调查，各自独立卡片、进度条、可最小化控制台
- [x] **重复打开去重**：再次打开已展开的历史报告时，直接滚动定位并高亮原卡片，不重复创建
- [x] **历史报告搜索**：按事故关键词（incident_id / service / severity）模糊搜索
- [x] **历史报告删除**：单条删除（悬停 🗑）与一键清空全部，均带二次确认
- [x] **静态资源防缓存**：页面与静态文件禁用浏览器缓存（no-store），功能更新即时可见

## 🛠️ 技术栈

- **智能体范式**：Plan-and-Solve、ReAct、Reflection
- **LLM**：任意 OpenAI 兼容 API（AIHubmix、ModelScope/Qwen、OpenAI、DeepSeek 等）
- **后端**：FastAPI + Uvicorn
- **存储**：JSON 故障剧本 + YAML runbook + **MySQL 5.7/8.x**（通过 PyMySQL 驱动）
- **前端**：原生 HTML/CSS/JS（零依赖，含手写迷你 Markdown 渲染器）

## 🚀 快速开始

### 环境要求

- Python 3.10+
- 本地运行中的 MySQL 5.7+（应用启动时自动创建 `sre_agent` 数据库和全部数据表）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置 API 密钥

```bash
cp .env.example .env
# 编辑 .env，设置 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL_ID
```

免费 LLM 选项：
- **DeepSeek**：`https://api.deepseek.com` — 国内直连，性价比高
- **AIHubmix**：`https://aihubmix.com/v1` — 免费额度，OpenAI 兼容
- **ModelScope/Qwen**：`https://api-inference.modelscope.cn/v1` — 每天 2000 次免费调用

### 配置 MySQL（可选）

连接参数通过环境变量配置，本地默认值如下：

```bash
MYSQL_HOST      # 默认 127.0.0.1
MYSQL_PORT      # 默认 3306
MYSQL_USER      # 默认 root
MYSQL_PASSWORD  # 默认空
MYSQL_DB        # 默认 sre_agent
```

Windows 上可在启动服务前临时设置（PowerShell）：

```powershell
$env:MYSQL_PASSWORD = "你的密码"
```

### 在 Jupyter Notebook 中运行

```bash
jupyter lab
# 打开 main.ipynb 依次运行全部单元格
```

### 启动 FastAPI 服务

```bash
uvicorn src.api.main:app --reload --port 8000
```

然后浏览器打开 **http://localhost:8000**：

- **登录页**（`/login.html`）：注册账号（需设置安全问题，用于找回密码）、登录、通过安全问题重置密码
- 登录后进入监控台：
  - 选择事故 → 点击 **开始调查** → 实时观看三阶段流水线运行
    （每个 `Thought` / `Action` / `Observation` 通过 Server-Sent Events 实时推送）。
    多个调查可**并发**运行——每个都有自己的卡片、**进度条**和**最小化**按钮。
  - 生成的复盘报告在 **复盘报告** 区域以 Markdown 渲染。
  - 每次完成的调查都会保存到 MySQL（`sre_agent.reports`）并出现在 **历史报告**
    列表中——点击即可重新打开；已打开的条目会滚动定位并高亮，不会重复创建。
  - **历史报告管理**：支持按事故关键词**搜索**（输入自动搜索或点「搜索」按钮）、
    单条**删除**（悬停 🗑）与一键**清空全部**（操作均需二次确认，删除不可恢复）。

API 端点一览：

| 方法 | 端点 | 说明 |
|---|---|---|
| `GET` | `/` | 监控台前端页面 |
| `GET` | `/login.html` | 登录 / 注册 / 找回密码页面 |
| `POST` | `/auth/register` | 注册账号（用户名、密码、安全问题、答案） |
| `POST` | `/auth/login` | 登录 → 返回 Bearer Token |
| `POST` | `/auth/logout` | 注销（吊销 Token） |
| `GET` | `/auth/me` | 校验 Token，返回用户名 |
| `POST` | `/auth/forgot/question` | 找回密码第一步：获取安全问题 |
| `POST` | `/auth/forgot/reset` | 找回密码第二步：验证答案并设置新密码 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/incidents/fixtures` | 列出可用事故样例（需认证） |
| `POST` | `/incidents/investigate` | 同步运行完整三阶段流水线（需认证） |
| `GET` | `/incidents/investigate/stream?incident_id=...&token=...` | SSE 流式运行流水线（需认证） |
| `GET` | `/incidents/{id}/report` | 获取本次会话生成的报告（需认证） |
| `GET` | `/incidents/reports` | 历史报告列表，支持 `?keyword=` 搜索（需认证） |
| `GET` | `/incidents/reports/{rid}` | 按运行 id 获取历史报告（需认证） |
| `DELETE` | `/incidents/reports/{rid}` | 按运行 id 删除单条历史报告（需认证） |
| `DELETE` | `/incidents/reports` | 清空全部历史报告（需认证，不可恢复） |

## 📖 使用示例

### 通过 Python（notebook 或脚本）

```python
from src.agents.pipeline import run_pipeline

result = run_pipeline("db_pool_exhaustion")
print(result["report"])    # Markdown 格式的 RCA 报告
print(result["findings"])  # 根因 + 证据字典
```

### 通过 API

```bash
# 列出可用事故
curl http://localhost:8000/incidents/fixtures

# 运行流水线
curl -X POST http://localhost:8000/incidents/investigate \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "db_pool_exhaustion"}'

# 获取生成的报告
curl http://localhost:8000/incidents/db_pool_exhaustion/report
```

### 输出示例

```
🚨 阶段 1：分诊 — 生成调查计划
   1. [log_search] pool exhausted — 查找数据库连接池耗尽相关日志
   2. [metric_query] db_pool — 检查连接池饱和度随时间变化
   3. [metric_query] latency — 量化请求延迟恶化程度
   4. [runbook_lookup] DB pool exhausted — 获取修复步骤

🔍 阶段 2：调查 — ReAct 工具循环
   步骤 1 — log_search[pool exhausted] → 找到 3 条匹配日志
   步骤 2 — metric_query[db_pool] → 连接池 14:01 起 10/10 满负荷
   步骤 3 — runbook_lookup[DB pool exhausted] → 已获取修复步骤
   ✅ 根因：orders.user_id 缺少索引导致全表扫描……

📝 阶段 3：复盘 — Reflection（草稿 → 评审 → 修订）
   质量评分：9/10 — 无需修订。
   ✅ 最终复盘报告完成。
```

## 🎯 项目亮点

- **一个系统串联三种智能体范式**：多数项目只使用单一范式；本项目将 Plan-and-Solve → ReAct → Reflection 串联为完整流水线
- **领域新颖性**：SRE/运维领域实践项目
- **贴近生产的剧本**：日志条目、指标时序、runbook YAML 都贴近真实故障模式（连接池耗尽、内存泄漏、限流级联）
- **预留升级路径**：FastAPI 后端已开 CORS；SSE 流式输出与前端界面均无需改动 Agent 核心代码

## 📊 性能评估

在 3 个故障样例上测试（Llama-3.3-70b / Groq，兼容任意 OpenAI 兼容 API）：

| 事故 | 根因识别 | 流水线耗时 |
|---|---|---|
| 数据库连接池耗尽 | ✅ orders.user_id 缺少索引 | ~30s |
| 内存泄漏 OOM | ✅ 会话缓存无 TTL/淘汰策略 | ~25s |
| 外部 API 限流 | ✅ 无指数退避的无限重试风暴 | ~28s |

样例根因识别准确率：**3/3（100%）**

## 🔮 未来计划

- [x] SSE 实时流：将 Agent 推理过程实时推送到前端
- [x] 前端监控台：事故选择器 + 实时追踪 + Markdown 查看器（原生 JS 已上线；框架升级可选）
- [ ] 真实日志接入：对接 Loki / CloudWatch / Datadog
- [ ] 向量记忆：对历史 RCA 报告做向量化，加速后续调查
- [ ] 安全的 runbook 执行：让 Agent 执行低风险修复命令
- [ ] Prometheus `/metrics` 端点 + Grafana 监控面板

## 📝 项目更新日志（中文注释）

> 记录本项目各阶段的功能迭代，便于了解项目演进历史。

- **2026-08 迭代二：历史报告管理与稳定性修复**
  - 历史报告支持关键词搜索（输入防抖 + 搜索按钮）、单条删除、一键清空（二次确认）
  - 静态资源禁用浏览器缓存（no-store），前端功能更新即时可见
  - 修复 Agent 调试打印在 GBK 控制台触发 UnicodeEncodeError 导致调查中断的问题
    （Agent 打印收归 verbose 控制 + stdout/stderr 强制 UTF-8 输出）
  - 修复历史报告详情接口 DATE_FORMAT 单 `%` 导致的 500 错误
  - SQLite 时代的历史报告已一次性迁移至 MySQL（原 `data/reports.db`）
- **2026-08 迭代一：监控台与存储层升级（本仓库维护者）**
  - 前端监控台上线：并发调查卡片、实时进度条、卡片最小化、重复打开自动跳转去重
  - 用户认证系统：注册 / 登录 / 登出 / 安全问题找回密码，PBKDF2 加盐哈希
  - 存储层从 SQLite 迁移到 MySQL：新增 `src/api/db.py` 统一封装，三张表启动自动建表
  - 修复登录页按钮无响应：`showMsg` 查询元素缺少 `#` 前缀，改用 `getElementById`
  - README 汉化，作者信息更新
- **项目初始版本（上游模板）**
  - 三阶段 Agent 流水线（Triage → Investigation → Postmortem）与三个故障样例

## 🤝 贡献指南

欢迎提交 Issue 与 PR！

## 📄 许可证

MIT License

## 👤 作者

- **GitHub**：[@kano321](https://github.com/kano321)
- **邮箱**：2587981962@qq.com

## 🙏 致谢

感谢 [Datawhale Hello-Agents](https://github.com/datawhalechina/hello-agents) 社区的优质课程，本项目构建于其第四章 ReAct、Plan-and-Solve、Reflection 示例之上。

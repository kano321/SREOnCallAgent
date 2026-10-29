# Kubernetes 部署说明

本目录包含用于三节点 Kind 集群的 Kustomize 部署基线。由于告警去重、SSE 订阅和会话报告目前保存在进程内存中，应用固定为一个副本。

## 环境准备

需要安装：

- Docker
- kubectl
- kind

## 创建集群并加载镜像

```bash
kind create cluster --config deploy/k8s/kind-config.yaml
docker build -t sre-oncall-agent:local .
kind load docker-image sre-oncall-agent:local --name sre-oncall
```

## 创建 Secret

先在当前终端设置真实环境变量，再创建两个 Secret：

```bash
kubectl create namespace sre-oncall --dry-run=client -o yaml | kubectl apply -f -

kubectl -n sre-oncall create secret generic sre-mysql-secret \
  --from-literal=MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-sre_root_dev}" \
  --from-literal=MYSQL_DB=sre_agent \
  --from-literal=MYSQL_USER=sre \
  --from-literal=MYSQL_PASSWORD="${MYSQL_PASSWORD:-sre_dev_password}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n sre-oncall create secret generic sre-app-secret \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --from-literal=LLM_BASE_URL="$LLM_BASE_URL" \
  --from-literal=LLM_MODEL_ID="$LLM_MODEL_ID" \
  --from-literal=WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-sre-webhook-dev}" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`secret.example.yaml` 只用于说明字段结构，不要把其中的占位值直接用于生产环境。

`configmap.yaml` 默认设置 `SESSION_COOKIE_SECURE=false`，以便通过本地 HTTP 端口转发演示。通过 HTTPS Ingress 对外提供服务时，应将其改为 `true`。

## 部署应用

```bash
kubectl apply -k deploy/k8s
kubectl -n sre-oncall rollout status statefulset/sre-mysql --timeout=180s
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent --timeout=180s
kubectl -n sre-oncall get pods,svc,pvc,ingress -o wide
```

部署内容包括：

- 命名空间 `sre-oncall`。
- MySQL StatefulSet、Headless Service 和 PVC。
- 应用 Deployment 和 ClusterIP Service。
- ConfigMap 和 Secret 引用。
- 启动、就绪和存活探针。
- CPU、内存请求与限制。
- 非 root 用户和只读根文件系统。
- 支持 SSE 长连接的 Ingress。

## 本地访问

Ingress 需要集群中安装 ingress-nginx 控制器。如果只做本地验证，可以直接使用端口转发：

```bash
kubectl -n sre-oncall port-forward service/sre-oncall-agent 8080:80
```

然后在另一个终端执行：

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/metrics
```

## 故障演练

删除应用 Pod，确认 Deployment 自动重建：

```bash
kubectl -n sre-oncall delete pod -l app.kubernetes.io/name=sre-oncall-agent
kubectl -n sre-oncall get pods -w
```

执行滚动重启：

```bash
kubectl -n sre-oncall rollout restart deployment/sre-oncall-agent
kubectl -n sre-oncall rollout status deployment/sre-oncall-agent --timeout=180s
```

如果发布失败，按以下顺序检查探针、事件、日志和指标：

```bash
kubectl -n sre-oncall describe pod -l app.kubernetes.io/name=sre-oncall-agent
kubectl -n sre-oncall logs deployment/sre-oncall-agent
kubectl -n sre-oncall get events --sort-by=.lastTimestamp
```

## 当前限制

- 应用只运行一个副本，因为去重、SSE 和会话报告仍保存在进程内存中。
- MySQL 是单个 StatefulSet 实例，不具备生产级高可用能力。
- Ingress 清单默认使用 ingress-nginx，本地验证可以不启用 Ingress。
- Pod 已配置 Prometheus 抓取注解，但集群中仍需单独部署 Prometheus Operator 或等价的抓取配置。
- 扩展到多副本前，必须先将共享状态迁移到 Redis，并完成跨副本一致性测试。
- 旧 Token 查询参数鉴权在 Kubernetes 配置中默认关闭，前端使用 HttpOnly Cookie 连接 SSE。

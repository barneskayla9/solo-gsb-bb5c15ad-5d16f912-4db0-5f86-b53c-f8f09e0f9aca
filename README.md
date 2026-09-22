# 灰度发布闸门 API（Gray Release Gate）

基于 **Python 3.11 + FastAPI + SQLite** 的灰度（金丝雀）发布闸门服务：

- 登记服务与版本，按百分比分阶段灰度；
- 基于 `sha256(service:user_id)` 的**稳定哈希**把用户分配到固定 cohort，提分阶段单调，用户不会在新旧版本间来回漂移；
- **租户定向放量**：创建 release 时可选 `tenant_allowlist`（定向白名单）与 `tenant_denylist`（拒绝名单）；`/route` 可携带 `tenant_id`，按 **拒绝名单 → 允许名单 → 稳定哈希百分比** 的优先级决策，响应与审计记录命中的规则；
- 接收成功 / 失败遥测，在**滑动时间窗口**内错误率超过阈值且样本数达标时**自动暂停**灰度，流量自动回退到当前稳定版本；
- 支持手动暂停、恢复、灰度回滚、服务整体回滚到上一个稳定版本；
- 完整的发布、上报、状态、审计接口。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由
│   ├── logic.py       # 哈希分流、滑动窗口、闸门评估、路由决策
│   ├── models.py      # Pydantic 请求模型
│   └── database.py    # SQLite 连接与建表
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

前置条件：已安装 Docker Engine 与 Docker Compose 插件（`docker --version`、`docker compose version` 可用）。

```bash
# 构建镜像并在后台启动（首次会构建镜像并自动建表 / 执行数据库迁移）
docker compose up -d --build

# 查看启动状态与日志（容器内置健康检查，15s 轮询 /health）
docker compose ps
docker compose logs -f canary-gate

# 验证
curl -s http://localhost:8080/health
```

- 服务端口：**`8080`**（宿主机与容器均为 8080，可在 `docker-compose.yml` 的 `ports` 中调整）
- 健康检查：`GET http://localhost:8080/health`（Compose 与 Dockerfile 均已配置 healthcheck）
- 交互式 API 文档（Swagger）：<http://localhost:8080/docs>，OpenAPI Schema：<http://localhost:8080/openapi.json>
- SQLite 数据持久化在 Docker volume `canary-data`（容器内 `/data/canary.db`，由 `CANARY_DB_PATH` 指定）
- 升级镜像后重新 `docker compose up -d --build` 即可，启动时会自动执行幂等的数据库迁移

停止：

```bash
docker compose stop           # 停止容器，保留容器与数据
docker compose down           # 删除容器，保留数据卷
docker compose down -v        # 连同数据卷一起删除（清空全部数据）
```

## 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

可通过环境变量覆盖数据库位置：`CANARY_DB_PATH=/tmp/canary.db`。

## 数据模型概览

| 表 | 说明 |
| --- | --- |
| `services` | 服务名、描述、默认灰度阶段（如 `[5,10,25,50,75,100]`） |
| `releases` | 版本、当前百分比、阶段计划、阈值/窗口/最小样本数、`tenant_allowlist`/`tenant_denylist`、状态（`progressing`/`paused`/`complete`/`rolled_back`） |
| `telemetry` | 每次上报的成功与否、延迟、状态码、用户、时间 |
| `audit_log` | 所有发布动作、路由决策（`route_resolved`，含命中规则）与自动暂停 / 回滚事件，含详细 JSON 上下文 |
| `schema_migrations` | 已执行的数据库迁移版本（用于旧库自动升级） |

**闸门规则**：在 `window_seconds`（默认 300s）滑动窗口内，当失败率 **严格大于** `error_threshold`（默认 0.1）且总请求数 ≥ `min_requests`（默认 10）时，灰度版本被自动置为 `paused`，路由全部回到稳定版本。首个登记的版本视为已知良好基线，直接 100% 上线。

**租户分流规则**：`/route` 携带 `tenant_id` 且存在进行中的灰度版本时，依次判定——① 租户在 `tenant_denylist` → 留在稳定版（`matched_rule=denylist`）；② 租户在 `tenant_allowlist` → 直达灰度版（`matched_rule=allowlist`）；③ 否则按 `sha256(service:user_id)` 的稳定哈希桶与当前百分比切分（`matched_rule=percentage`）。灰度被暂停时任何名单都不生效（`matched_rule=paused`）。

**数据库迁移**：服务启动时（`init_db`）自动建表并执行幂等迁移，记录在 `schema_migrations` 表中。已有旧版数据库（无 `tenant_allowlist`/`tenant_denylist` 列）重启后会通过 migration 1 自动补列，存量 release 默认两个名单均为 `[]`，无需手工干预。

## 示例 curl（完整流程）

### 1. 登记服务（自定义灰度阶段）

```bash
curl -s -X POST http://localhost:8080/services \
  -H 'Content-Type: application/json' \
  -d '{"name":"checkout","description":"结算服务","default_rollout":[10,25,50,100]}'
```

### 2. 发布基线版本 v1.0.0（首个版本自动 100%）

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v1.0.0"}'
```

### 3. 发布灰度版本 v2.0.0（从 10% 开始，错误率阈值 20%，窗口 300s）

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2.0.0","error_threshold":0.2,"window_seconds":300,"min_requests":10}'
```

返回中 `id` 即灰度版本 ID（下例假设为 `2`）。

发布时也可携带租户名单进行定向放量：

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2.1.0",
       "tenant_allowlist":["tenant-acme","tenant-beta"],
       "tenant_denylist":["tenant-frozen"]}'
```

- `tenant_allowlist`：名单内租户**始终命中灰度版本**，绕过百分比切分（灰度被暂停时除外）；
- `tenant_denylist`：名单内租户**始终留在稳定版本**，优先级高于白名单；
- 两个名单均为可选；条目会自动去空白、去重；**同一租户同时出现在两个名单时返回 `422 Unprocessable Entity`**；
- 名单随 release 持久化，`GET .../releases` 会返回这两个字段。

### 4. 查询某用户被路由到哪个版本（稳定哈希分流）

```bash
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123"
# cohort=canary -> v2.0.0；cohort=stable -> v1.0.0；同一用户结果恒定
```

携带 `tenant_id` 时启用租户定向，决策优先级为 **拒绝名单 → 允许名单 → 稳定哈希百分比**：

```bash
# 命中拒绝名单：无论哈希结果如何都留在稳定版
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-frozen"
# {"cohort":"stable","version":"v1.0.0","matched_rule":"denylist", ...}

# 命中允许名单：即使哈希落在 10% 之外也直达灰度版
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-acme"
# {"cohort":"canary","version":"v2.1.0","matched_rule":"allowlist", ...}

# 不在任何名单上：回落到稳定哈希百分比分流
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-other"
# {"matched_rule":"percentage", ...}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `cohort` | `canary` / `stable` / `none`（`none` 时 HTTP 503） |
| `matched_rule` | 命中的规则：`denylist`、`allowlist`、`percentage`、`paused`；无灰度时为 `null` |
| `tenant_id` | 回显本次查询使用的租户标识；未传时为 `null` |
| `bucket` | `sha256(service:user_id) % 100` 的稳定哈希桶，用于百分比分流 |

**未提供 `tenant_id` 时完全保持原有行为**（仅按稳定哈希百分比切分），老调用方无需改动。每次路由查询都会写入一条 `route_resolved` 审计记录，其中包含 `tenant_id`、`cohort`、`version` 与 `matched_rule`，可在 `/audit?action=route_resolved` 查询。

### 5. 上报遥测（单条）

成功：

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases/2/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-123","success":true,"latency_ms":42,"status_code":200}'
```

失败：

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases/2/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-123","success":false,"latency_ms":800,"status_code":500}'
```

批量（每条事件需带 `release_id`）：

```bash
curl -s -X POST http://localhost:8080/services/checkout/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"events":[
    {"release_id":2,"success":true,"status_code":200},
    {"release_id":2,"success":false,"status_code":500}
  ]}'
```

快速制造超阈值（10 次中 5 次失败 → 错误率 0.5 > 0.2）：

```bash
for i in $(seq 1 5); do
  curl -s -o /dev/null -X POST http://localhost:8080/services/checkout/releases/2/telemetry \
    -H 'Content-Type: application/json' -d '{"success":false,"status_code":500}'
done
for i in $(seq 1 5); do
  curl -s -o /dev/null -X POST http://localhost:8080/services/checkout/releases/2/telemetry \
    -H 'Content-Type: application/json' -d '{"success":true,"status_code":200}'
done
```

最后一次上报的响应中会出现 `"auto_paused": true`；之后 `/route` 全部回到 `v1.0.0`。

### 6. 查看闸门状态与窗口错误率

```bash
curl -s http://localhost:8080/services/checkout/status
```

### 7. 推进 / 暂停 / 恢复 / 回滚

```bash
# 推进到下一阶段（10% -> 25% -> 50% -> 100%）
curl -s -X POST http://localhost:8080/services/checkout/releases/2/promote \
  -H 'Content-Type: application/json' -d '{}'

# 直接推进到指定百分比
curl -s -X POST http://localhost:8080/services/checkout/releases/2/promote \
  -H 'Content-Type: application/json' -d '{"percentage":50}'

# 手动暂停 / 恢复（错误率仍超阈值时恢复会被拒绝）
curl -s -X POST "http://localhost:8080/services/checkout/releases/2/pause?reason=hotfix"
curl -s -X POST http://localhost:8080/services/checkout/releases/2/resume

# 回滚灰度版本（流量立即回到稳定版本，该版本标记 rolled_back）
curl -s -X POST http://localhost:8080/services/checkout/releases/2/rollback

# 服务整体回滚：当前稳定版降级，恢复到上一个稳定版
curl -s -X POST http://localhost:8080/services/checkout/rollback
```

### 8. 查询审计日志

```bash
curl -s "http://localhost:8080/services/checkout/audit"
curl -s "http://localhost:8080/services/checkout/audit?action=auto_paused&limit=20"
# 查询路由决策审计（查看每个请求命中的租户规则）
curl -s "http://localhost:8080/services/checkout/audit?action=route_resolved&limit=20"
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/services` | 登记服务 |
| GET | `/services` | 服务列表 |
| POST | `/services/{name}/releases` | 发布版本（首个自动成为稳定基线；可选 `tenant_allowlist`/`tenant_denylist`，冲突返回 422） |
| GET | `/services/{name}/releases` | 版本列表（含租户名单） |
| GET | `/services/{name}/route?user_id=&tenant_id=` | 用户路由决策（canary / stable）；优先级：拒绝名单 → 允许名单 → 稳定哈希百分比；响应含 `matched_rule`，并写入 `route_resolved` 审计 |
| POST | `/services/{name}/releases/{id}/promote` | 推进灰度阶段 |
| POST | `/services/{name}/releases/{id}/pause` | 手动暂停 |
| POST | `/services/{name}/releases/{id}/resume` | 恢复灰度 |
| POST | `/services/{name}/releases/{id}/rollback` | 回滚灰度到稳定版 |
| POST | `/services/{name}/rollback` | 服务回滚到上一稳定版 |
| POST | `/services/{name}/releases/{id}/telemetry` | 上报单条遥测 |
| POST | `/services/{name}/telemetry` | 批量上报遥测 |
| GET | `/services/{name}/status` | 闸门状态 + 滑动窗口指标 |
| GET | `/services/{name}/audit` | 审计查询（可按 action / version 过滤） |
| GET | `/health` | 健康检查 |

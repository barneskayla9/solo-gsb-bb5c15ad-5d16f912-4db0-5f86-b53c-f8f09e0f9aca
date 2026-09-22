# 灰度发布闸门 API（Gray Release Gate）

基于 **Python 3.11 + FastAPI + SQLite** 的灰度（金丝雀）发布闸门服务：

- 登记服务与版本，按百分比分阶段灰度；
- 基于 `sha256(service:user_id)` 的**稳定哈希**把用户分配到固定 cohort，提分阶段单调，用户不会在新旧版本间来回漂移；
- **租户定向放量**：发布时可设置 `tenant_allowlist`（恒走灰度）与 `tenant_denylist`（恒走稳定），`/route` 携带 `tenant_id` 时按 **拒绝名单 → 允许名单 → 稳定哈希百分比** 优先级决策，响应与审计记录命中规则；
- 接收成功 / 失败遥测，在**滑动时间窗口**内错误率超过阈值且样本数达标时**自动暂停**灰度，流量自动回退到当前稳定版本；
- 支持手动暂停、恢复、灰度回滚、服务整体回滚到上一个稳定版本；
- 完整的发布、上报、状态、审计接口。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由
│   ├── logic.py       # 哈希分流、租户名单、滑动窗口、闸门评估、路由决策
│   ├── models.py      # Pydantic 请求模型（含名单冲突校验）
│   └── database.py    # SQLite 连接、建表与幂等迁移
├── scripts/
│   └── verify_tenant_routing.py   # 租户定向放量端到端自检脚本
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

前置条件：已安装 Docker 20.10+（含 Compose v2，即 `docker compose` 子命令）。

```bash
docker compose up -d --build
```

首次启动会：

1. 构建镜像（基于 `python:3.11-slim`，仅安装 `requirements.txt`）；
2. 在 Docker volume `canary-data` 中初始化 SQLite 数据库（容器内路径 `/data/canary.db`，可通过环境变量 `CANARY_DB_PATH` 修改）；
3. 启动后自动执行幂等数据库迁移——**旧版本数据库无需手工处理**，缺失的租户名单列会在启动时自动补齐（历史版本的名单视为空列表）；
4. 开启容器健康检查（轮询 `/health`），可通过 `docker compose ps` 查看 `healthy` 状态。

- 服务端口：**`8080`**
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger）：<http://localhost:8080/docs>
- ReDoc 文档：<http://localhost:8080/redoc>
- SQLite 数据持久化在 Docker volume `canary-data`（容器内 `/data/canary.db`）

常用命令：

```bash
docker compose logs -f          # 跟踪日志
docker compose restart          # 重启（数据保留，启动时自动迁移）
docker compose down             # 停止并删除容器，保留数据卷
docker compose down -v          # 连同数据卷一起删除（清空所有数据）
```

端口被占用时，可编辑 `docker-compose.yml` 中的端口映射（如 `"18080:8080"`）。

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
| `releases` | 版本、当前百分比、阶段计划、阈值/窗口/最小样本数、租户名单（`tenant_allowlist` / `tenant_denylist`，JSON 数组）、状态（`progressing`/`paused`/`complete`/`rolled_back`） |
| `telemetry` | 每次上报的成功与否、延迟、状态码、用户、时间 |
| `audit_log` | 所有发布动作、租户路由判定（`route_checked`）与自动暂停 / 回滚事件，含详细 JSON 上下文 |

> **数据库迁移**：建表语句已包含租户名单列；对升级前创建的旧数据库，服务启动时会通过幂等的 `ALTER TABLE ... ADD COLUMN`（SQLite 不支持 `IF NOT EXISTS`，代码先用 `PRAGMA table_info` 检查列是否存在）自动补齐，旧行默认两个名单均为 `[]`。

**闸门规则**：在 `window_seconds`（默认 300s）滑动窗口内，当失败率 **严格大于** `error_threshold`（默认 0.1）且总请求数 ≥ `min_requests`（默认 10）时，灰度版本被自动置为 `paused`，路由全部回到稳定版本。首个登记的版本视为已知良好基线，直接 100% 上线。

## 租户定向放量（tenant allowlist / denylist）

发布灰度版本时可选两个租户名单（均为字符串数组，元素去空白、去重，单项最长 128）：

- `tenant_allowlist`：名单内租户**恒走灰度版本**，即使其稳定哈希桶落在百分比之外（0% 灰度也可定向命中）；
- `tenant_denylist`：名单内租户**恒走稳定版本**，即使灰度已达 100%（可作为租户级紧急开关）；
- 同一租户同时出现在两个名单属于配置冲突，`POST .../releases` 返回 **422**，响应体给出冲突的租户；
- 两个名单只对**进行中的灰度**（`progressing`，含尚未 promote 完成的 100% 灰度）生效；灰度 promote 完成成为稳定版后名单自然失效；灰度 `paused` 时允许名单不能绕过暂停（全部走稳定），拒绝名单依然优先生效。

`/route` 增加可选查询参数 `tenant_id`：

- **携带 `tenant_id`** 时，路由优先级为 **拒绝名单 → 允许名单 → 稳定哈希百分比分流**；响应新增 `tenant_id` 与 `rule` 字段，并写入一条 `route_checked` 审计记录；
- **不携带 `tenant_id`** 时，请求行为与响应结构与旧版完全一致（仅按 `user_id` 哈希分流，不产生路由审计记录）。

`rule` 取值：

| rule | 含义 |
| --- | --- |
| `tenant_denied` | 命中拒绝名单，固定走稳定版（无稳定版时 503，`rule=none_available`） |
| `tenant_allowed` | 命中允许名单，固定走灰度版（灰度暂停时不适用，记为 `canary_paused`） |
| `canary_cohort` / `stable_cohort` | 未命中名单，按稳定哈希桶是否落在灰度百分比内分流 |
| `canary_full` | 灰度已达 100% 但尚未完成，非拒绝租户全部走灰度 |
| `canary_paused` | 灰度已暂停，全部走稳定 |
| `stable_only` | 无进行中的灰度，由稳定版提供服务 |
| `none_available` | 没有任何可服务版本（返回 503） |

示例：

```bash
# 发布带租户定向的灰度版本
curl -s -X POST http://localhost:8080/services/checkout/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2.0.0","tenant_allowlist":["tenant-vip"],"tenant_denylist":["tenant-freeze"]}'

# 允许名单租户：无论哈希桶如何都走灰度
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-vip"
# -> {"cohort":"canary","version":"v2.0.0","rule":"tenant_allowed", ...}

# 拒绝名单租户：固定稳定版
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-freeze"
# -> {"cohort":"stable","version":"v1.0.0","rule":"tenant_denied", ...}

# 普通租户：沿用稳定哈希百分比
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-other"
# -> rule 为 canary_cohort 或 stable_cohort

# 名单冲突 -> 422
curl -i -X POST http://localhost:8080/services/checkout/releases -H 'Content-Type: application/json' \
  -d '{"version":"v2.1.0","tenant_allowlist":["t1"],"tenant_denylist":["t1"]}'

# 审计中查看每次租户路由判定命中的规则
curl -s "http://localhost:8080/services/checkout/audit?action=route_checked"
```

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

如需**租户定向放量**，可在请求体中追加名单（详见下文「租户定向放量」）：

```bash
curl -s -X POST http://localhost:8080/services/checkout/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2.0.1","tenant_allowlist":["tenant-vip"],"tenant_denylist":["tenant-freeze"]}'
```

### 4. 查询某用户被路由到哪个版本（稳定哈希分流）

```bash
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123"
# cohort=canary -> v2.0.0；cohort=stable -> v1.0.0；同一用户结果恒定

# 携带租户标识时，按 拒绝名单 -> 允许名单 -> 稳定哈希百分比 决策，
# 响应中的 rule 标明命中的规则
curl -s "http://localhost:8080/services/checkout/route?user_id=user-123&tenant_id=tenant-vip"
```

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
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/services` | 登记服务 |
| GET | `/services` | 服务列表 |
| POST | `/services/{name}/releases` | 发布版本（首个自动成为稳定基线；可选 `tenant_allowlist` / `tenant_denylist`，冲突返回 422） |
| GET | `/services/{name}/releases` | 版本列表 |
| GET | `/services/{name}/route?user_id=&tenant_id=` | 用户路由决策（canary / stable）；携带 `tenant_id` 时按 拒绝→允许→哈希百分比 决策并返回命中 `rule` |
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

# 离线车队回执中继 —— API

独立模块，仅依赖 Python 标准库。默认挂载前缀 `/relay`（页面 `/relay/`，接口 `/relay/api/...`）。
所有写操作在全局锁内串行；后台转发器共用同一把锁。

## 值班身份与岗位权限

除现场终端接口（`/terminal/...`）与 `/health` 外，管理接口均需值班身份：
请求头 `X-Relay-User: <username>`（或 JSON 字段 `_user`）。三类岗位权限：

| 权限 | 含义 | 可操作 |
|---|---|---|
| `observe` 观察 | 只读 | 批次/队列/审计/归档/审批记录查看、回执上传 |
| `dispatch` 调度 | 敏感指令（调度类） | 执行包导入、失败重试、终态解除重投（申请与批准） |
| `archive_review` 归档审查 | 敏感指令（封存类） | 批次封存（申请与批准） |

三类敏感指令**默认强制双人复核**（`RELAY_ENFORCE_APPROVALS=1`，默认开）：
不再直接执行业务，而是先形成**待批事项**，由**另一个**具备相应权限的账号批准。
关闭时（`=0`）恢复直调语义（兼容旧客户端/集成测试），此时不强制登录。

### 账号管理
- `GET /relay/api/accounts` — 账号与岗位列表。
- `POST /relay/api/accounts`
  `{"username","display_name?","roles":["observe","dispatch","archive_review"],"active?","note?"}`
  新建或调整；**停用账号或收缩角色会使其名下未决事项立即失效**。
  设置了环境变量 `RELAY_ADMIN_TOKEN` 时需请求头 `X-Relay-Admin-Token`。
- `POST /relay/api/accounts/{username}/active` `{"active":true|false}`。
- `GET /relay/api/me` — 当前身份、权限（未登录时返回可选账号提示）。

首次启动自动播种演示账号：`zhang_obs`(观察)、`li_disp`/`wang_disp`(调度)、
`zhao_arch`/`sun_arch`(归档审查)。

### 双人复核（待批事项）

待批事项 `approvals` 携带：
- **请求指纹** `request_hash = sha256(canonical{action, params, requested_by})`：
  同指纹重复申请返回同一事项（幂等）；
- **资源版本** `resource_version = sha256(canonical(申请瞬间目标资源状态))`：
  执行包维度（存在性/内容哈希/封存标记）；批次维度（游标 + 每条回执状态/结果码 + 封存标记）。

接口：
- `POST /relay/api/approvals` — 提交申请，体：
  - `{"action":"package_import","package":{...},"note?"}`
  - `{"action":"manual_retry","batch_ref","seq?","note?"}`
  - `{"action":"ack_terminal","batch_ref","seq","note?"}`
  - `{"action":"batch_seal","batch_ref","note?"}`
  需对应岗位权限；201 新建、200 幂等重放。
- `POST /relay/api/approvals/{id}/decide`
  `{"decision":"approve|reject","note?"}` — 另一账号复核。

批准前置（任一不满足即拒）：
1. 事项仍 `pending`；批准人具备所需岗位；**批准人 ≠ 申请人**（`403 self_approval_forbidden`）；
2. 申请人账号未停用且仍具所需权限（否则事项置 `invalid`，`409 approval_invalidated`）；
3. 未超时（默认 15 分钟，`RELAY_APPROVAL_TTL`；原因 `timeout`）；
4. **目标资源版本未变化**（否则置 `invalid`，原因 `resource_changed`）。

语义保证：
- 并发批准用 compare-and-swap（`UPDATE ... WHERE status='pending'`），
  **只有一次生效并真正执行业务**，其它并发批准得到第一次结果的幂等回放；
- 重复申请/重复批准均幂等；已 `executed/rejected` 的同指纹申请回放原事项；
  已 `invalid/execute_failed` 的同指纹允许重提新事项（旧记录保留）；
- 批准后在同一事务语义内调用**原业务函数**，不绕过任何业务校验：
  业务拒绝（如不可封存、终态未解除、不可变冲突）→ 事项 `execute_failed`，
  返回原状态码与错误码，业务侧零副作用；
- 账号停用、角色调整、资源变化、超时使未决事项**立即失效但不回滚已生效动作**；
- 全部过程写完整决策记录，服务重启后待办/失效/决策完整恢复。

- `GET /relay/api/approvals/todo` — 本人待办：
  `my_requests[]`（我申请未决）、`awaiting_my_review[]`（待我复核，非本人且有权限）。
- `GET /relay/api/approvals?status=pending,invalid,executed,...&mine=1` — 事项列表
  （含已失效事项；视图按当前身份标注 `can_approve`）。
- `GET /relay/api/approvals/{id}` — 事项详情 + `decisions[]` 决策时间线。
- `GET /relay/api/approval-decisions` — 完整决策记录
  （`requested/approved/rejected/invalidated/executed/execute_failed`，只追加）。

> 强制复核开启时，直接调用下面的敏感指令端点返回
> **403 `approval_required`**（带申请/批准端点提示）。


## 顺序与恰好一次模型

- 每个批次独立游标 `cursors.next_seq`（初始为执行包 `seq_base`，通常 1）。
- 回执状态机：
  `queued`（就绪）/`buffered`（缺口缓冲）/`dispatching`（回调中，带租约）
  /`forwarded`（终态成功，永不重调）/`retry_wait`（瞬时失败退避中）
  /`rejected_terminal`（终态拒绝，**冻结游标**）。
- 序号大于游标的回执**持久缓冲**；缺口补齐后，缓冲项自动转 queued 并连续转发。
- 转发幂等键：`{batch_ref}:{seq}:{内容哈希前16位}`，回调重试/重发放送同一键。
- `dispatching` 带 30s 租约（`RELAY_LEASE_SECONDS`）；进程崩溃后重启回收过期租约为 queued。
  `forwarded` 行永不参与回收 → 已成功回调绝不重复调用。

## 管理/操作 API

### 执行包导入（不可变、幂等；双人复核）
`POST /relay/api/packages/import`（默认需先经 `/approvals` 双人复核，见上）
```json
{"package": {"batch_ref":"B-001","batch_summary":"...","seq_base":1,
             "vehicle":{...},"destination":{...},"items":[...]}}
```
- 201 首次存档；200 `replay:true` 为**完全相同**内容的再次导入（返回原结果）。
- 同 `batch_ref` 内容不同 → **409 package_conflict**，原包永不覆盖，记冲突。
- 必需字段：`batch_ref`、`batch_summary`；`seq_base` 默认 1。

### 批次视图
- `GET /relay/api/batches` — 批次卡片：`cursor`、`gaps[]`、`buffered_items[]`、
  各状态计数、`blocker`（终态拒绝项）、`status`。
- `GET /relay/api/batches/{batch_ref}` — 详情：全部回执（状态、尝试次数、
  `result_code`/`result_detail`/`last_error`、时间）、执行包原文。

### 回执上传
- `POST /relay/api/receipts` — 单条：
```json
{"batch_ref":"B-001","seq":1,"receipt_no":"R-0001",
 "type":"departed|failed_to_depart|arrived","batch_summary":"...",
 "at":"2026-10-02T08:00:00Z"}
```
- `POST /relay/api/receipts/batch` 或
  `POST /relay/api/batches/{batch_ref}/receipts` — 批量：
```json
{"batch_ref":"B-001","batch_summary":"...",
 "receipts":[{...},{...}]}
```
  批量为**原子事务**：任一条被拒整批不生效（已插入的先回滚），仅留拒绝审计。

判定规则：

| 情况 | 结果 |
|---|---|
| 未知批次 | 404 `unknown_batch`，记冲突 |
| 批次已封存 | 409 `batch_archived`（完全相同的历史回执按归档 `duplicate_replay` 重放） |
| 批次摘要不匹配 | 409 `summary_mismatch`，记冲突 |
| 完全相同回执（按规范化内容哈希）再次提交 | 200 `duplicate_replay`，返回原结果 |
| 同 `seq` 内容不同 | 409 `conflict_seq`，**不推进游标** |
| 同 `receipt_no` 被不同内容/序号使用 | 409 `conflict_receipt_no` |
| `seq == next_seq` | `queued`，后台立即转发 |
| `seq > next_seq` | `buffered`，缺口补齐前不转发 |
| `seq < next_seq` 且无原记录 | 409 `seq_behind_cursor` |

### 队列 / 重试（敏感指令，双人复核）
- `GET /relay/api/queue` — 所有 queued/buffered/retry_wait/dispatching/终态拒绝行。
- `POST /relay/api/retries` `{"batch_ref","seq?","note?"}`
  — 默认需先经 `/approvals`（`action=manual_retry`）由另一调度账号批准；
  批准后把退避中的失败项立即重新入队；终态拒绝项返回 **409 terminal_requires_ack**。
- `POST /relay/api/batches/{ref}/receipts/{seq}/ack-terminal`
  `{"note":"..."}` — 默认需经 `/approvals`（`action=ack_terminal`）双人复核；
  **人工确认主系统终态已解除**（批次恢复/车辆或目的地改回）后把终态拒绝项重投；
  全程审计，游标仍按序推进。

### 审计
- `GET /relay/api/audit` — 审计流：导入/重放/冲突/派发/成功/退避重试/终态拒绝/
  手动重试/租约回收/启动恢复/封存与归档校验。
- `GET /relay/api/conflicts` — 被拒冲突留痕（同序异容、同号异容、摘要不符、未知批次、
  已封存批次的回执）。

## 磁盘回收：批次封存与只读归档

中继磁盘按批次回收空间。只有**无缺口、无待重试/阻塞项且全部回执已结束
（forwarded）**的批次允许封存；封存把热库回执明细与该批次审计行迁入只读归档，
原审计时间线完整保留在归档内。

- 封存是**单个 SQLite 事务**：内容清单 + 校验摘要 + 归档写入 + 热库删除 + 封存标记
  同生共死；中途失败/进程崩溃整体回滚，不留半迁移状态。
- 幂等：同一批次重复封存返回第一次结果（`replay:true`，同 digest/sealed_at）。
- 封存后该批次新回执一律 **409 batch_archived**；完全相同的历史回执按归档
  `duplicate_replay` 重放——**归档记录永不再次进入转发队列**。
- 批次详情 `GET /relay/api/batches/{ref}` 对已封存批次自动从归档恢复只读视图
  （`readonly:true`，含回执明细与原审计时间线）。

### 预估占用
`GET /relay/api/admin/storage`
```json
{"batches":[{"batch_ref":"B-001","archived":false,"sealable":true,
             "reasons":[],"receipt_count":3,"receipt_bytes":900,
             "audit_count":10,"audit_bytes":450,"hot_bytes":1350}],
 "totals":{"hot_bytes":2232,"archive_bytes":0,"freed_bytes":0,"sealable":1,"archived":0}}
```
不可封存时 `reasons[]` 逐条给出原因：`has_gaps`（含缺口序号）、
`pending_queued/dispatching/retry_wait/buffered/rejected_terminal`。

### 执行封存（幂等，单事务；双人复核）
`POST /relay/api/batches/{ref}/seal` `{"note?":"月底空间回收"}`
默认需先经 `/approvals`（`action=batch_seal`）由另一归档审查账号批准；
批准时重算封存资格与校验摘要，不绕过原封存校验。
- 200：`{sealed:true, replay:false, digest, manifest, receipt_count, audit_count,
  freed_bytes, sealed_at}`；重复调用 → `replay:true` 并返回第一次结果。
- 进行中批次 → **409 batch_not_sealable**（`reasons[]` 同预估接口）。
- 封存前完整性校验（逐条重算执行包/回执内容哈希）失败 → **409 checksum_mismatch**
  （`errors[]` 指明不一致的条目），热库数据原样保留。

### 归档列表 / 校验并恢复只读详情 / 下载
- `GET /relay/api/archives` — 归档列表（digest、封存时间、回执/审计条数、回收字节）。
- `GET /relay/api/archives/{ref}` — 重算校验摘要并恢复只读详情：
  `{readonly:true, verification:{ok,...}, manifest, package, receipts[], audit[]}`。
  摘要不一致 → **409 archive_corrupt**，拒绝恢复并记审计。
- `GET /relay/api/archives/{ref}/download` — 下载完整归档 JSON
  （`Content-Disposition: attachment`）。离线校验：去掉 `digest/sealed_at/
  sealed_by/freed_bytes` 附加字段后，对剩余对象做规范化（键排序、无空白）
  JSON 序列化并取 SHA-256，应等于 `digest`。

## 现场终端 API（只读）

- `GET /relay/api/terminal/manifest` — 可下载批次清单（含内容哈希与下载地址）。
- `GET /relay/api/terminal/packages/{batch_ref}` — 下载不可变执行包，
  内嵌 `relay.content_hash` 与 `immutable:true`，重复下载字节一致。

## 回调适配器契约

适配器 `deliver(package, receipt, idempotency_key)` 返回三态：

| 结果 | 含义 | 中继行为 |
|---|---|---|
| `ack` | 主系统受理 | forwarded，游标 +1，连续转发下一项 |
| `retry` | 超时/网络/408/429/5xx | retry_wait，指数退避（1→2→4…，上限 60s，可配置） |
| `terminal` | 批次已撤回 / 车辆或目的地已变化 | rejected_terminal，游标冻结 |

实现：
- `stub`（默认）：进程内桩，可按 `batch:seq`/`batch:*`/`*` 注入
  `ack|timeout|network_error|http_5xx|batch_withdrawn|vehicle_changed|
  destination_changed|fail_then_ok[:N]`。
- `http`：POST `RELAY_CALLBACK_URL`，携带 `X-Idempotency-Key`；
  主系统 200 `{"result":"ack"}` 为成功，
  `{"result":"rejected","reason":"batch_withdrawn|vehicle_changed|destination_changed"}`
  为终态拒绝。

## 运维 API
- `GET /relay/api/health` — 适配器类型、转发统计、启动回收数。
- `POST /relay/api/admin/stub` `{"rules":{...},"reset_calls":false}`
  — 注入桩规则（仅 stub 适配器）。
- `POST /relay/api/admin/state` — 适配器状态 + 桩最近调用记录（验证恰好一次）。
- `POST /relay/api/admin/recover` — 手动触发租约回收（启动时已自动执行）。

## 环境变量
`RELAY_PORT`(8090) `RELAY_HOST` `RELAY_DB`(./data/relay.db)
`RELAY_CALLBACK_ADAPTER`(stub|http) `RELAY_CALLBACK_URL`
`RELAY_CALLBACK_TIMEOUT`(8s) `RELAY_TICK_SECONDS`(1)
`RELAY_LEASE_SECONDS`(30) `RELAY_BACKOFF_BASE`(1) `RELAY_BACKOFF_CAP`(60)
`RELAY_ENFORCE_APPROVALS`(1，敏感指令强制双人复核；置 0 恢复直调且不强制登录)
`RELAY_APPROVAL_TTL`(900 秒，未决事项有效期)
`RELAY_ADMIN_TOKEN`(空=本地演示开放账号管理；非空时账号写接口需 X-Relay-Admin-Token)

# API 说明

所有接口均在全局事务锁内串行执行；写接口成功后数据修订号 `revision` 单调递增。
请求/响应均为 `application/json; charset=utf-8`。

## 资源模型

| 对象 | 关键字段 | 说明 |
|---|---|---|
| 网格 `grids` | `code,name,x,y,threatened` | 受威胁网格是路网节点与家庭位置 |
| 道路 `roads` | `code,a,b,length,capacity,status` | 无向边；`capacity`=单波次可并发通过的车辆数；`status=open/closed` |
| 避难点 `shelters` | `code,name,x,y,beds,accessible,pets_allowed,status` | `status=active/suspended` |
| 车辆 `vehicles` | `code,name,seats,accessible,status` | `status=available/down/departed` |
| 家庭 `households` | `ref,name,grid,members,mobility,pets,contact` | `ref` 为外部户号，**家庭不可拆分**，重复 `ref` 不重复登记 |

容量口径：床位按**人数**计；车辆座位按 `members + pets`（宠物占座）；
道路容量按**车辆批次**计；已出发（`departed`）占用永久锁定。

## 接口

### 状态
- `GET /api/state` — 全量态势：网格/道路/避难点/车辆/家庭、批次与安置、
  每户状态、各避难点 `committed/reserved/remaining` 床位、修订号、最近事件。
- `GET /api/events` — 审计事件流。

### 导入与登记（幂等）
- `POST /api/import/grids` `/roads` `/shelters` `/vehicles`
  - 体：对象数组（或 `{"items":[...]}`）。按 `code` upsert，重复导入覆盖更新、不产生重复行。
- `POST /api/households`
  - 单个对象或数组；同 `ref` 已存在则跳过（`skipped_duplicates` 计数，且不覆盖原登记）。

### 规划与发布
- `POST /api/plan` `{"name","commander"}`
  - 仅对**受威胁网格中尚未出发**的家庭求解；返回
    `feasible,max_wave,batches[],unassigned[],capacity,totals` 及方案 `id`。
  - 不改变任何已发布命令，可反复调用以比较候选方案。
- `GET /api/plans` / `GET /api/plans/{id}` — 候选与历史方案列表/详情。
- `POST /api/plans/{id}/publish` `{"commander"}`
  - 发布分区撤离令。乐观并发：方案 `base_revision` 必须等于当前 `revision`，
    否则返回 **409**（已有他人发布，或路况/资源已变化），需重新规划。
  - 不可行方案、空方案、违反硬约束的方案一律 409 拒绝。
  - 发布即替换全部**未出发**计划批次；已出发批次与其床位占用原样保留。

### 执行（承诺）
- `POST /api/batches/{id}/depart`
  - 车辆离场：批次置 `departed`、车辆置 `departed`、目的地床位计入 `committed`。
    同一辆车后续未出发批次自动撤回（`cancelled`）等待重新编排。
  - **承诺不可逆**：之后任何事件与重新规划都不会改派该户或释放其床位。

### 态势事件（只影响未出发批次）
`POST /api/events`，按 `type`：
- `road_close {"code"}` — 封闭道路；路径经过该道路的未出发批次撤回。
- `road_open {"code"}` — 恢复道路，之后可继续编排。
- `vehicle_down / vehicle_recovered {"code"}`
- `shelter_suspend / shelter_resume {"code"}`
- `fire_expand {"code","close_roads":[...]}` — 火线扩大，可同时封路。
- `fire_clear {"code"}` — 网格解除威胁。

返回均含 `revision` 与 `released_for_replan`（被撤回待重排的户号）。

### 运维
- `POST /api/reset` — 清空并恢复演示数据（同时清空推演分支元数据与分支库文件）。

## 推演分支（隔离沙盒）

分支是**独立 SQLite 文件**（创建时对实战库做完整物理备份）。分支下的所有写操作
只落在分支库，实战库不受任何影响；提升(promote)是唯一把推演资源改动合并回实战的通道。
分支地址统一为 `/api/branches/{id}/...`；分支内写接口体可带 `op_id`（客户端生成的
幂等键），相同 `op_id` 的重试返回首次结果，不重复执行。

### 分支管理
- `POST /api/branches` `{"name","idem_key?"}`
  - 从**当前实战态势**复制出隔离分支；返回 `id`、`base_live_revision`（创建基线修订号）。
  - `idem_key` 相同的重复创建返回同一分支（`idempotent_replay:true`）。
- `GET /api/branches` — 分支列表（id/name/status/创建基线/上次提升修订号/时间）。
- `POST /api/branches/{id}/discard` — 废弃分支（幂等）。废弃后禁止再写；
  分支文件与审计保留，差异仍可只读查看。
- `GET /api/branches/{id}/audit` — 分支生命周期审计
 （create / simulate / promote_ok / promote_rejected / discard，含操作细节与 op_id）。

### 分支内连续模拟（全部隔离、幂等、可重启恢复）
分支支持的写接口与实战同构（路径前加 `/api/branches/{id}`）：
- `POST /api/branches/{id}/events`（态势事件，体可含 `op_id`）
- `POST /api/branches/{id}/plan`
- `POST /api/branches/{id}/plans/{pid}/publish`
- `POST /api/branches/{id}/batches/{bid}/depart`

只读：`GET .../state`、`GET .../plans`、`GET .../plans/{pid}`、`GET .../events`。
返回体带 `context:{kind:"branch",id,name,...}` 以明确上下文。

### 并排差异
- `GET /api/branches/{id}/diff`
  - `people[]`：每户在实战/推演的去向（status/shelter/wave，`differs` 标记）；
  - `shelters[]`：两侧床位 `beds/committed/reserved/remaining/status`；
  - `blockers[]`：两侧最新方案的受阻原因（按户）；
  - `resource_fields[]`：三方（base/live/branch）逐字段判定
    （`branch_only` 可提升 / `live_only` / `same_change` / `commitment` / `conflict`）；
  - 附双方修订号、命令数、已离场/待安排户数汇总。

### 提升（三方冲突检查）
- `POST /api/branches/{id}/promote`
  `{"expected_live_revision":<打开差异时看到的实战修订号>, "promote_id":"<幂等键>"}`
  - **按创建基线**对每个被跟踪字段（道路 status/capacity、避难点 status/beds、
    车辆 status、网格 threatened）做 base/实战/推演三方比较：
    - 仅推演改过（实战相对基线未动）→ 干净改动，应用；
    - 两边改成相同值 → 无冲突、无需应用；
    - 两边都改且值不同 → 冲突；
  - `expected_live_revision` 与当前实战修订号不一致 → **过期基线，409 stale_baseline**；
  - 存在任一冲突 → **整体拒绝（409 conflict）**，不写任何实战数据；
    返回 `conflict_objects`（`表:编号`）与 `conflicts[]`（对象、字段、base/live/branch 三值），
    以及 `pending_clean_changes`；
  - 成功时返回 `applied_fields`、新 `revision`、因状态变化而撤回待重排的户、
    `skipped_commitments`；
  - **只迁移资源字段**：推演产生的命令、批次、离场记录、已承诺床位/车辆占用永不带入实战
    （分支内车辆 `departed` 标记为 `commitment` 自动忽略）；
  - 相同 `promote_id` 重放返回首次结果（成功或被拒），凭证持久化、重启后仍有效。

## 硬约束（规划器与发布前校验器双重保证）

1. 床位：`已承诺 + 本方案安置人数 ≤ 总床位`；
2. 座位：`成员 + 宠物数 ≤ 车辆座位`；
3. 行动辅助户只能乘无障碍车辆、进入无障碍避难点；
4. 携带宠物户只能进入 `pets_allowed` 避难点；
5. 路径必须全部由 `open` 道路连续构成（系统不安排家庭自行经过关闭路段）；
6. 同一波次同一条道路上的批次数 ≤ 道路容量；同一车辆同一波次最多一批；
7. 暂停接收的避难点、故障/已离场车辆不得使用；
8. 每个待撤离家庭恰好安置一次；已出发家庭不得被重新分配。

无可行解时，`unassigned[]` 按户给出具体阻断原因（封路、无障碍车不足、
座位不足、某避难点剩余床位、户间竞争耗尽床位、道路时序瓶颈等）。

## 离线车队回执中继对接面

独立模块（[relay/](relay/)，端口 8090）经下列接口与本系统连接：

- `GET /api/execution-packages?status=scheduled` — 可导出批次列表。
- `GET /api/execution-package/v1/{batch_id}` — 不可变执行包
  （`batch_ref`=批次 id，含摘要、车辆、目的地、路径、搭载家庭；
  首次导出在 `execution_exports` 留车辆/目的地快照）。
- `POST /api/relay-callback` — 中继按序转发的回执：
  - 按 `idempotency_key` 幂等，重复调用返回首次响应（台账 `relay_callbacks`）；
  - 批次不存在/已撤回 → `{"result":"rejected","reason":"batch_withdrawn"}`；
  - 车辆相对导出快照被改派或已故障下线 → `vehicle_changed`；
  - 目的地被改派或避难点已暂停 → `destination_changed`；
  - 合法回执返回 `{"result":"ack"}`；`departed` 类联动既有离场承诺语义。

中继自身 API（执行包导入、终端下载、批量回执、重试、审计）见 [relay/API.md](relay/API.md)。

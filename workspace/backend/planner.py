"""撤离编排规划器（零第三方依赖）。

将"家庭"作为不可拆分单元，在满足以下硬约束的前提下分配到避难点与撤离批次：
  1. 床位：同一避难点安置人数不超过剩余床位（已出发家庭占用的床位永久锁定）；
  2. 车辆座位：单批次搭载成员+宠物数不超过车辆座位；
  3. 无障碍：行动辅助家庭只能乘无障碍车辆、进入无障碍避难点；
  4. 道路：只能走开放道路，同一波次通过任一道路的批次数不超过其容量；
  5. 接收条件：避难点须处于接收状态，携带宠物的家庭只能进入允许宠物的避难点；
  6. 一个批次 = 一辆车 + 一个出发网格 + 一个目的地避难点 + 一条路径。

目标：在最小波次内完成全部安置（DFS 回溯，按 K=1,2,... 递增）。
已出发（departed）家庭及其资源占用不参与重算。
"""
import heapq
import json

# 搜索节点上限，防止极端数据下回溯爆炸；超出后按不可行处理并给出诊断
SEARCH_NODE_CAP = 300_000


# ---------------------------------------------------------------- 图与路径

def build_graph(rows):
    """rows: 道路行列表；返回 邻接表 与 道路索引（仅开放道路参与通行）。"""
    adj = {}
    roads = {}
    for r in rows:
        roads[r["code"]] = dict(r)
        if r["status"] != "open":
            continue
        adj.setdefault(r["a"], []).append(r)
        adj.setdefault(r["b"], []).append(r)
    return adj, roads


def _other(road, node):
    return road["b"] if road["a"] == node else road["a"]


def dijkstra(adj, src, dst, banned_edges=()):
    """返回 (距离, 节点路径, 道路编号路径)；不可达返回 None。"""
    banned = set(banned_edges)
    dist = {src: 0.0}
    prev = {}  # node -> (road_code, prev_node)
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        if u == dst:
            break
        for road in adj.get(u, ()):
            if road["code"] in banned:
                continue
            v = _other(road, u)
            nd = d + float(road["length"])
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (road["code"], u)
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None
    edge_path, node_path = [], [dst]
    cur = dst
    while cur != src:
        code, p = prev[cur]
        edge_path.append(code)
        node_path.append(p)
        cur = p
    node_path.reverse()
    edge_path.reverse()
    return dist[dst], node_path, edge_path


def k_paths(adj, src, dst, k=3):
    """最短路 + 依次删除最短路上各边求得的备选简单路（供道路容量分流）。"""
    first = dijkstra(adj, src, dst)
    if not first:
        return []
    found = [first]
    seen = {tuple(first[2])}
    for edge in list(first[2]):
        alt = dijkstra(adj, src, dst, banned_edges={edge})
        if alt and tuple(alt[2]) not in seen:
            seen.add(tuple(alt[2]))
            found.append(alt)
        if len(found) >= k:
            break
    found.sort(key=lambda p: p[0])
    return found


# ---------------------------------------------------------------- 状态快照

class Snapshot:
    def __init__(self, db):
        c = db.conn
        self.grids = {r["code"]: dict(r) for r in c.execute("SELECT * FROM grids")}
        self.shelters = {r["code"]: dict(r) for r in c.execute("SELECT * FROM shelters")}
        self.roads = {r["code"]: dict(r) for r in c.execute("SELECT * FROM roads")}
        self.vehicles = {r["code"]: dict(r) for r in c.execute("SELECT * FROM vehicles")}
        self.households = {r["ref"]: dict(r) for r in c.execute("SELECT * FROM households")}
        self.adj, self.open_roads = build_graph(
            [r for r in self.roads.values() if r["status"] == "open"]
        )

        # 已出发家庭 → 锁定信息（重算时不可触碰，床位/车辆永久占用）
        self.locked = {}
        rows = c.execute(
            """
            SELECT a.household_ref AS ref, b.shelter_code AS shelter,
                   b.vehicle_code AS vehicle, b.wave AS wave
            FROM assignments a JOIN batches b ON a.batch_id = b.id
            WHERE b.status = 'departed'
            """
        )
        for r in rows:
            self.locked[r["ref"]] = dict(r)

        # 待规划家庭：处于受威胁网格且尚未出发
        self.to_plan = [
            h for ref, h in self.households.items()
            if h["grid"] in self.grids
            and self.grids[h["grid"]]["threatened"]
            and ref not in self.locked
        ]

        # 已锁定床位（按人数计；宠物不占床位但占座位）
        self.committed_beds = {code: 0 for code in self.shelters}
        for ref, info in self.locked.items():
            self.committed_beds[info["shelter"]] += self.households[ref]["members"]

        # 已离场车辆不可再用
        self.locked_vehicles = {info["vehicle"] for info in self.locked.values()}

        # 预计算每个网格 → 每个避难点的候选路径
        self.paths = {}  # (grid, shelter) -> [(dist, nodes, edges)]
        for g in self.grids:
            for s in self.shelters:
                ps = k_paths(self.adj, g, s, k=3)
                if ps:
                    self.paths[g, s] = ps


def household_size(h):
    """车辆座位需求 = 成员 + 宠物（每只宠物按 1 个座位计）。"""
    return h["members"] + h["pets"]


def eligible_shelters(snap, h):
    """返回该家庭按接收条件可进入的避难点代码列表（不含床位/路径判断）。"""
    out = []
    for code, s in snap.shelters.items():
        if s["status"] != "active":
            continue
        if h["mobility"] and not s["accessible"]:
            continue
        if h["pets"] and not s["pets_allowed"]:
            continue
        out.append(code)
    return out


def usable_vehicles(snap, h):
    """可到该家庭所在网格接人的车辆（无障碍匹配、未故障、未离场）。"""
    out = []
    for code, v in snap.vehicles.items():
        if v["status"] != "available" or code in snap.locked_vehicles:
            continue
        if h["mobility"] and not v["accessible"]:
            continue
        out.append(code)
    return sorted(out, key=lambda c: (-snap.vehicles[c]["seats"], c))


# ---------------------------------------------------------------- 回溯搜索

class Group:
    """一个撤离批次（同网格、同避难点、同车、同波次）。"""
    __slots__ = ("wave", "vehicle", "grid", "shelter", "route", "dist",
                 "seats_used", "households")

    def __init__(self, wave, vehicle, grid, shelter, route, dist):
        self.wave = wave
        self.vehicle = vehicle
        self.grid = grid
        self.shelter = shelter
        self.route = route          # 道路编号列表
        self.dist = dist
        self.seats_used = 0
        self.households = []


def plan(snap, max_wave=None):
    """执行规划，返回方案 dict。"""
    todo = snap.to_plan
    K_max = max_wave or max(1, len(todo))

    # 预过滤：为每户准备 (避难点->候选路径) 与可选车辆，按"安置难度"排序
    prepared = []
    for h in todo:
        reachable = {
            s: snap.paths[h["grid"], s]
            for s in eligible_shelters(snap, h)
            if (h["grid"], s) in snap.paths
        }
        prepared.append((h, reachable, usable_vehicles(snap, h)))
    prepared.sort(key=lambda p: (len(p[1]), -household_size(p[0])))

    result = None
    for K in range(1, K_max + 1):
        st = _SearchState(snap, prepared, K)
        groups = st.run()
        if groups is not None:
            result = groups
            K_used = K
            break
    else:
        K_used = None

    if result is not None:
        return _render(snap, result, K_used, [h for h, _, _ in prepared], [])
    unassigned = diagnose(snap, prepared)
    return _render(snap, [], None, [], unassigned)


def _diagnose_one(snap, h):
    """单户维度的阻断原因。"""
    reasons = []
    size = household_size(h)

    # 1) 道路封闭导致无路可达
    any_path = any(
        (h["grid"], s) in snap.paths for s in snap.shelters
        if snap.shelters[s]["status"] == "active"
    )
    if not any_path:
        reasons.append("通往所有避难点的道路均已关闭，家庭无法自行或乘车经开放道路撤离")

    # 2) 避难点接收条件（符合条件的才报告床位，避免无关噪音）
    eligible = []
    for code, s in snap.shelters.items():
        blocks = []
        if s["status"] != "active":
            blocks.append("避难点已暂停接收")
        if h["mobility"] and not s["accessible"]:
            blocks.append("无无障碍通道")
        if h["pets"] and not s["pets_allowed"]:
            blocks.append("不接收宠物")
        if blocks:
            if (h["grid"], code) in snap.paths:
                reasons.append(f"避难点 {s['name']}（{code}）不可用：{'、'.join(blocks)}")
        else:
            eligible.append(code)

    # 3) 车辆
    avail = [v for v in snap.vehicles.values()
             if v["status"] == "available" and v["code"] not in snap.locked_vehicles]
    if not avail:
        reasons.append("没有可用车辆（全部故障或已离场）")
    else:
        if h["mobility"] and not any(v["accessible"] for v in avail):
            reasons.append("该户需要无障碍车辆，但已无可用无障碍车")
        if not any(v["seats"] >= size for v in avail
                   if (not h["mobility"] or v["accessible"])):
            reasons.append(f"没有座位数≥{size}（含{h['pets']}只宠物占位）的可用车辆")

    # 4) 床位（仅报告该户单看即不足的确定性短缺）
    bed_ok = []
    for code in eligible:
        s = snap.shelters[code]
        free = s["beds"] - snap.committed_beds.get(code, 0)
        if free >= h["members"] and (h["grid"], code) in snap.paths:
            bed_ok.append(code)
        elif free < h["members"]:
            reasons.append(f"避难点 {s['name']}（{code}）剩余床位 {max(free,0)}，"
                           f"不足该户 {h['members']} 人")

    # 5) 道路容量（单独开一批次也挤不进 → 容量瓶颈）
    vehicles = usable_vehicles(snap, h)
    if bed_ok and vehicles:
        fit_vehicle = next((v for v in vehicles
                            if snap.vehicles[v]["seats"] >= size), None)
        edge_blocked = True
        if fit_vehicle:
            for s in bed_ok:
                for dist, nodes, edges in snap.paths[h["grid"], s]:
                    if all(snap.open_roads[e]["capacity"] >= 1 for e in edges):
                        edge_blocked = False
                        break
                if not edge_blocked:
                    break
        if edge_blocked and fit_vehicle:
            bottlenecks = sorted({
                f"{snap.roads[e]['code']}（容量 {snap.roads[e]['capacity']}/批次）"
                for s in bed_ok
                for _, _, edges in snap.paths[h["grid"], s]
                for e in edges if snap.open_roads[e]["capacity"] < 1
            })
            reasons.append("受道路单批次容量限制无法编排" +
                           ("：" + "、".join(bottlenecks) if bottlenecks else ""))
    return reasons, eligible, bed_ok


def diagnose(snap, prepared):
    """为无法安置的家庭逐条说明阻断约束（含户间资源竞争）。"""
    placement, starved, relaxed_failed = relaxed_greedy(snap)
    failed_refs = {h["ref"] for h in relaxed_failed}
    out = []
    for h, reachable, vehicles in prepared:
        reasons, eligible, bed_ok = _diagnose_one(snap, h)

        # 户间竞争：放宽道路时序后该户仍无位 → 床位总量在竞争中耗尽
        if h["ref"] in failed_refs and not any("床位" in r for r in reasons):
            for code in starved:
                s = snap.shelters[code]
                reasons.append(
                    f"避难点 {s['name']}（{code}）床位总量不足："
                    f"{s['beds']} 张中已承诺 {snap.committed_beds.get(code,0)} 张，"
                    "其余待撤离家庭按难度优先分配后已无空余床位")

        # 放宽后可安置、精确搜索仍失败 → 道路时序/波次容量或车辆同时性瓶颈
        if not reasons:
            reasons.append("单户资源均可满足，但在当前道路单批次容量与车辆同时性约束下，"
                           "无法在有限波次内排出完整方案；建议解封备用道路、增加车辆，"
                           "或等待已出发车辆返程后继续编排后续波次")

        out.append({
            "ref": h["ref"], "name": h["name"], "grid": h["grid"],
            "members": h["members"], "mobility": h["mobility"],
            "pets": h["pets"], "reasons": reasons,
        })
    return out


class _SearchState:
    def __init__(self, snap, prepared, K):
        self.snap = snap
        self.prepared = prepared
        self.K = K
        self.beds = dict(snap.committed_beds)
        self.groups = []
        self.edge_used = {}            # (wave, road) -> 批次数
        self.nodes = 0

    def run(self):
        return self._dfs(0)

    def _route_fits(self, wave, route, cap_extra=None):
        for code in route:
            cap = self.snap.open_roads[code]["capacity"]
            cur = self.edge_used.get((wave, code), 0) + (1 if code in (cap_extra or ()) else 0)
            if cur + (0 if cap_extra else 0) >= cap and not cap_extra:
                return False
            if cap_extra and code in cap_extra:
                if self.edge_used.get((wave, code), 0) + 1 > cap:
                    return False
        return True

    def _edges_have_capacity(self, wave, route):
        for code in route:
            cap = self.snap.open_roads[code]["capacity"]
            if self.edge_used.get((wave, code), 0) >= cap:
                return False
        return True

    def _add_edges(self, wave, route, delta):
        for code in route:
            key = (wave, code)
            self.edge_used[key] = self.edge_used.get(key, 0) + delta

    def _dfs(self, idx):
        if idx == len(self.prepared):
            return self.groups
        self.nodes += 1
        if self.nodes > SEARCH_NODE_CAP:
            return None

        h, reachable, vehicles = self.prepared[idx]
        size = household_size(h)
        snap = self.snap

        # 候选动作：(优先级键, 类型, 载荷)
        actions = []

        # 动作一：加入同网格/同避难点、座位与道路容量均允许的既有批次
        for gi, g in enumerate(self.groups):
            if g.grid != h["grid"] or g.shelter not in reachable:
                continue
            veh = snap.vehicles[g.vehicle]
            # 无障碍车辆属性必须满足车上每一户（含拼车后上的户）
            if h["mobility"] and not veh["accessible"]:
                continue
            if g.seats_used + size > veh["seats"]:
                continue
            if self.beds.get(g.shelter, 0) + h["members"] > snap.shelters[g.shelter]["beds"]:
                continue
            actions.append((g.dist, 0, gi))

        # 动作二：开新批次，占用某个波次的某辆车
        used_in_wave = {(g.wave, g.vehicle) for g in self.groups}
        for shelter, paths in sorted(reachable.items(), key=lambda kv: kv[1][0][0]):
            need_beds = h["members"]
            if self.beds.get(shelter, 0) + need_beds > snap.shelters[shelter]["beds"]:
                continue
            for w in range(1, self.K + 1):
                for veh_code in vehicles:
                    if (w, veh_code) in used_in_wave:
                        continue
                    veh = snap.vehicles[veh_code]
                    if veh["seats"] < size:
                        continue
                    for dist, nodes, edges in paths:
                        if self._edges_have_capacity(w, edges):
                            actions.append((dist + 1000.0, 1,
                                            (w, veh_code, shelter, edges, dist)))
                            break  # 同车同波次只取第一条可容路径

        # 优先拼车、再近远排序
        actions.sort(key=lambda a: a[0])

        beds_before = dict(self.beds)
        for _, kind, payload in actions:
            if kind == 0:
                # 合乘到既有批次：道路容量按"车辆批次"计算，
                # 同一批次多上一户不增加道路占用，故不改动 edge_used。
                g = self.groups[payload]
                g.seats_used += size
                g.households.append(h["ref"])
                self.beds[g.shelter] += h["members"]
                if self._dfs(idx + 1) is not None:
                    return self.groups
                self.beds = dict(beds_before)
                g.households.pop()
                g.seats_used -= size
            else:
                w, veh_code, shelter, edges, dist = payload
                g = Group(w, veh_code, h["grid"], shelter, edges, dist)
                g.seats_used = size
                g.households.append(h["ref"])
                self.groups.append(g)
                self.beds[shelter] = self.beds.get(shelter, 0) + h["members"]
                self._add_edges(w, edges, 1)
                used_in_wave.add((w, veh_code))
                if self._dfs(idx + 1) is not None:
                    return self.groups
                self._add_edges(w, edges, -1)
                used_in_wave.discard((w, veh_code))
                self.beds = dict(beds_before)
                self.groups.pop()
        return None


# ---------------------------------------------------------------- 放宽竞争检查

def relaxed_greedy(snap):
    """忽略波次/道路容量与车辆"每波只能一趟"限制（车辆可无限往返），
    仅保留床位、座位、无障碍、接收条件、路径可达性，做贪心安置。

    用于诊断：若放宽后仍无法安置，说明是床位/车辆/接收条件等硬总量不足，
    而非道路时序容量问题。返回 (安置映射 hh_ref->shelter, 每户床位需求满足情况,
    床位被耗尽的避难点集合, 未能安置的家庭)。
    """
    beds = dict(snap.committed_beds)
    # 车辆按可用状态分组；座位容量可无限次使用（多趟），故只记录座位与无障碍属性
    veh_pool = [snap.vehicles[c] for c in snap.vehicles
                if snap.vehicles[c]["status"] == "available"
                and c not in snap.locked_vehicles]
    placement = {}
    starved = set()
    failed = []
    # 难度大的家庭优先
    order = sorted(snap.to_plan,
                   key=lambda h: (len(eligible_shelters(snap, h)), -household_size(h)))
    for h in order:
        placed = False
        candidates = []
        for code in eligible_shelters(snap, h):
            if (h["grid"], code) not in snap.paths:
                continue
            free = snap.shelters[code]["beds"] - beds.get(code, 0)
            if free >= h["members"]:
                candidates.append((free, code))
        # 车辆可行性
        fit_veh = [v for v in veh_pool
                   if v["seats"] >= household_size(h)
                   and (not h["mobility"] or v["accessible"])]
        for _, code in sorted(candidates):
            if fit_veh:
                beds[code] = beds.get(code, 0) + h["members"]
                placement[h["ref"]] = code
                placed = True
                break
        if not placed:
            failed.append(h)
            if not fit_veh and candidates:
                pass  # 车辆问题由逐户诊断说明
            elif not candidates and fit_veh:
                for code in eligible_shelters(snap, h):
                    if (h["grid"], code) in snap.paths:
                        starved.add(code)
    return placement, starved, failed


# ---------------------------------------------------------------- 独立校验

def validate_plan(snap, batches_payload):
    """发布前对方案做独立复核（与搜索逻辑解耦的安全网）。

    返回违规原因列表；空列表表示方案满足全部硬约束。
    """
    errors = []
    seen_hh = set()
    bed_delta = {}
    wave_edge_batches = {}
    used_veh_wave = set()

    for b in batches_payload:
        veh = snap.vehicles.get(b["vehicle"])
        sh = snap.shelters.get(b["shelter"])
        if not veh:
            errors.append(f"批次{b['seq']}引用了不存在的车辆 {b['vehicle']}")
            continue
        if not sh:
            errors.append(f"批次{b['seq']}引用了不存在的避难点 {b['shelter']}")
            continue
        if veh["status"] != "available" or b["vehicle"] in snap.locked_vehicles:
            errors.append(f"批次{b['seq']}使用了不可用/已离场车辆 {b['vehicle']}")
        if sh["status"] != "active":
            errors.append(f"批次{b['seq']}目的地 {sh['name']} 已暂停接收")

        seats = 0
        people = 0
        mobility_onboard = False
        for hh in b["households"]:
            h = snap.households.get(hh["ref"])
            if not h:
                errors.append(f"批次{b['seq']}含不存在的家庭 {hh['ref']}")
                continue
            if hh["ref"] in seen_hh:
                errors.append(f"家庭 {hh['ref']} 被重复分配（家庭不可拆分/重复安置）")
            seen_hh.add(hh["ref"])
            if h["grid"] != b["grid"]:
                errors.append(f"批次{b['seq']}从{b['grid']}出发，但家庭 {hh['ref']} 位于 {h['grid']}")
            seats += household_size(h)
            people += h["members"]
            if h["mobility"]:
                mobility_onboard = True
                if not veh["accessible"]:
                    errors.append(f"行动辅助家庭 {hh['ref']} 被安排到非无障碍车辆 {b['vehicle']}")
                if not sh["accessible"]:
                    errors.append(f"行动辅助家庭 {hh['ref']} 被安排到无无障碍设施的 {sh['name']}")
            if h["pets"] and not sh["pets_allowed"]:
                errors.append(f"携带宠物家庭 {hh['ref']} 被安排到不接收宠物的 {sh['name']}")
        if seats > veh["seats"]:
            errors.append(f"批次{b['seq']}车辆 {b['vehicle']} 座位 {seats}>{veh['seats']}（含宠物占位）")
        bed_delta[b["shelter"]] = bed_delta.get(b["shelter"], 0) + people

        if (b["wave"], b["vehicle"]) in used_veh_wave:
            errors.append(f"车辆 {b['vehicle']} 在第{b['wave']}波被重复使用")
        used_veh_wave.add((b["wave"], b["vehicle"]))

        # 路径必须由开放道路连续组成（禁止经过已关闭路段，也不允许自行前往）
        edges = b.get("route_edges", [])
        expect = b["grid"]
        for code in edges:
            road = snap.open_roads.get(code)
            if not road:
                errors.append(f"批次{b['seq']}路径经过已关闭/不存在道路 {code}（家庭不得自行经过关闭路段）")
                continue
            if expect not in (road["a"], road["b"]):
                errors.append(f"批次{b['seq']}路径在 {expect} 与道路 {code} 不连续")
            expect = road["b"] if road["a"] == expect else road["a"]
            wave_edge_batches[(b["wave"], code)] = wave_edge_batches.get((b["wave"], code), 0) + 1
        if expect != b["shelter"]:
            errors.append(f"批次{b['seq']}路径终点 {expect} 与目的地 {b['shelter']} 不一致")

    for (w, code), n in wave_edge_batches.items():
        cap = snap.open_roads[code]["capacity"]
        if n > cap:
            errors.append(f"第{w}波道路 {code} 并发批次 {n} 超过容量 {cap}")

    for code, add in bed_delta.items():
        total = snap.committed_beds.get(code, 0) + add
        if total > snap.shelters[code]["beds"]:
            errors.append(f"避难点 {snap.shelters[code]['name']} 床位 {total}>{snap.shelters[code]['beds']}（含已承诺占用）")

    planned_refs = {h["ref"] for h in snap.to_plan}
    missing = planned_refs - seen_hh
    if missing:
        errors.append(f"以下待撤离家庭未被安置：{sorted(missing)}")
    locked_overlap = seen_hh & set(snap.locked)
    if locked_overlap:
        errors.append(f"试图重新分配已出发家庭：{sorted(locked_overlap)}")
    return errors


# ---------------------------------------------------------------- 方案渲染

def _render(snap, groups, K_used, assigned, unassigned):
    batches = []
    reserved_beds = {code: 0 for code in snap.shelters}
    groups_sorted = sorted(groups, key=lambda g: (g.wave, g.vehicle, g.shelter))
    for seq, g in enumerate(groups_sorted, 1):
        nodes = [g.grid]
        for code in g.route:
            road = snap.roads[code]
            nodes.append(road["b"] if road["a"] == nodes[-1] else road["a"])
        households = [
            {"ref": ref, **{k: snap.households[ref][k]
                            for k in ("name", "members", "mobility", "pets")}}
            for ref in g.households
        ]
        batches.append({
            "seq": seq,
            "wave": g.wave,
            "vehicle": g.vehicle,
            "vehicle_name": snap.vehicles[g.vehicle]["name"],
            "grid": g.grid,
            "shelter": g.shelter,
            "shelter_name": snap.shelters[g.shelter]["name"],
            "route_edges": g.route,
            "route_nodes": nodes,
            "distance": round(g.dist, 2),
            "seats_used": g.seats_used,
            "seats_total": snap.vehicles[g.vehicle]["seats"],
            "households": households,
        })
        reserved_beds[g.shelter] += sum(x["members"] for x in households)

    capacity = {}
    for code, s in snap.shelters.items():
        committed = snap.committed_beds.get(code, 0)
        reserved = reserved_beds.get(code, 0)
        capacity[code] = {
            "name": s["name"], "beds": s["beds"],
            "committed": committed, "reserved": reserved,
            "remaining": s["beds"] - committed - reserved,
            "status": s["status"],
        }

    return {
        "feasible": not unassigned and bool(groups),
        "max_wave": K_used,
        "batches": batches,
        "unassigned": unassigned,
        "capacity": capacity,
        "totals": {
            "households_planned": len(snap.to_plan),
            "households_locked": len(snap.locked),
            "households_assigned": sum(len(b["households"]) for b in batches),
            "vehicles_used": len({b["vehicle"] for b in batches}),
        },
    }

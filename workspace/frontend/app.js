/* 山火应急撤离编排系统 —— 前端（原生 JS，无构建依赖） */
"use strict";

let S = null;
let branchesCache = [];
let diffCache = null;

// ------------------------------------------------------------ 上下文（实战 / 推演分支）
// Ctx: {kind:'live'} 或 {kind:'branch', id, name, status, base}
const Ctx = {
  get() {
    try { return JSON.parse(sessionStorage.getItem("evac_ctx") || '{"kind":"live"}'); }
    catch { return { kind: "live" }; }
  },
  set(c) { sessionStorage.setItem("evac_ctx", JSON.stringify(c)); },
  clear() { sessionStorage.removeItem("evac_ctx"); },
  get isBranch() { return this.get().kind === "branch"; },
};

const basePath = () => Ctx.isBranch ? `/api/branches/${Ctx.get().id}` : "";
const newOpId = () => "op_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

const WAVE_COLORS = ["#5aa9ff", "#b388ff", "#ffd166", "#4cc38a", "#ff7ab0", "#7be3d4"];
const waveColor = (w) => WAVE_COLORS[(w - 1) % WAVE_COLORS.length];

const api = async (path, body) => {
  // 分支上下文：自动把调用路由到分支 API；写操作补 op_id 幂等键
  let url = path;
  let payload = body;
  if (Ctx.isBranch) {
    const isBranchScoped = path.startsWith("/api/branches/");
    if (!isBranchScoped && path.startsWith("/api/")) {
      url = `${basePath()}${path}`;
      if (body !== undefined) payload = { ...body, op_id: body.op_id || newOpId() };
    }
  }
  const opt = payload === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload) };
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.error || `HTTP ${r.status}`);
    err.payload = data;
    err.status = r.status;
    throw err;
  }
  return data;
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const toast = (msg, kind = "") => {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ""; }, 3600);
};

const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });

// ---------------------------------------------------------------- 数据加载

async function refresh() {
  try {
    const prevRev = S ? S.revision : null;
    S = await api("/api/state");
    document.getElementById("revision").textContent = S.revision;
    const warn = document.getElementById("revWarn");
    warn.hidden = !(prevRev !== null && prevRev !== S.revision);
    renderContextBanner();
    renderAll();
    if (document.getElementById("tab-sandbox").classList.contains("active")) {
      loadDiff();
    }
  } catch (e) {
    if (e.status === 409 && e.payload?.code === "discarded") {
      toast("当前分支已被废弃，已切回实战上下文", "err");
      exitToLive();
    } else {
      toast("无法连接服务：" + e.message, "err");
    }
  }
}

function renderAll() {
  renderMap();
  renderCapacity();
  renderVehicles();
  renderSelectors();
  renderBatches();
  renderHouseholds();
  renderLog();
  renderPlansList(); // 轻量摘要
  renderSandbox();
  applyContextGuards();
}

// ------------------------------------------------------------ 上下文标识

function renderContextBanner() {
  const bar = document.getElementById("ctxBar");
  const ctx = Ctx.get();
  if (ctx.kind === "live") {
    bar.className = "ctx-bar live";
    bar.innerHTML = `<span class="ctx-dot live"></span><b>实战上下文（LIVE）</b>
      <span class="muted">所有写入立即生效于真实调度数据</span>
      <span style="margin-left:auto"></span>`;
    return;
  }
  const discarded = ctx.status === "discarded";
  bar.className = "ctx-bar " + (discarded ? "discarded" : "branch");
  bar.innerHTML = `<span class="ctx-dot ${discarded ? "discarded" : "branch"}"></span>
    <b>${discarded ? "已废弃推演分支（只读）" : "推演沙盒（ISOLATED）"}</b>
    <span class="muted">${esc(ctx.name || ctx.id)} · ${esc(ctx.id)} · 创建基线 r${ctx.base ?? "?"}</span>
    <span style="margin-left:auto"></span>
    <button class="mini" onclick="exitToLive()">返回实战</button>`;
}

function applyContextGuards() {
  const branch = Ctx.isBranch;
  const discarded = branch && Ctx.get().status === "discarded";
  // 住户登记 / 资源导入 / 重置 只在实战上下文开放（分支只复制创建基线，不承载新增登记）
  ["hAdd", "importBtn", "resetBtn"].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.disabled = branch; el.title = branch ? "推演分支内不支持新增登记/导入，请在实战上下文操作" : ""; }
  });
  // 分支内所有模拟操作（规划/发布/离场/态势事件）在废弃后禁用
  ["publishBtn", "planBtn", "replanBtn", "roadCloseBtn", "roadOpenBtn", "vehDownBtn",
   "vehRecoverBtn", "shelSuspendBtn", "shelResumeBtn", "fireBtn", "fireClearBtn"]
    .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = discarded; });
  document.getElementById("householdsPanel").style.opacity = branch ? ".55" : "";
  document.getElementById("resourcesPanel").style.opacity = branch ? ".55" : "";
  const note = document.getElementById("ctxWriteNote");
  if (note) note.hidden = !branch;
}

window.exitToLive = () => {
  Ctx.clear();
  currentPlanId = null;
  switchTab(Ctx._lastLiveTab || "map");
  refresh();
};

window.enterBranch = async (id, name, base, status) => {
  Ctx.set({ kind: "branch", id, name, base, status: status || "active" });
  currentPlanId = null;
  switchTab("map");
  await refresh();
  toast(`已进入隔离推演分支：${name}（所有模拟不会影响实战）`, "ok");
};

// ---------------------------------------------------------------- 地图

function nodeMap() {
  const m = {};
  S.grids.forEach(g => m[g.code] = { ...g, kind: "grid" });
  S.shelters.forEach(s => m[s.code] = { ...s, kind: "shelter" });
  return m;
}

function hhCountByGrid() {
  const cnt = {};
  S.households.forEach(h => {
    const st = S.household_state[h.ref];
    if (st && st.status !== "departed") cnt[h.grid] = (cnt[h.grid] || 0) + 1;
  });
  return cnt;
}

function renderMap() {
  const svg = document.getElementById("map");
  const nodes = nodeMap();
  const waiting = hhCountByGrid();
  let html = "";

  // 道路
  S.roads.forEach(r => {
    const a = nodes[r.a], b = nodes[r.b];
    if (!a || !b) return;
    html += `<line class="road ${r.status}" data-road="${esc(r.code)}"
      x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}">
      <title>${esc(r.code)} ${esc(r.a)}↔${esc(r.b)} 容量${r.capacity}/批次 — ${r.status === "open" ? "开放（点击封闭）" : "已封闭（点击恢复）"}</title></line>`;
  });

  // 批次路线（已承诺实线，待出发虚线）
  const wavesSeen = new Set();
  S.batches.forEach(b => {
    if (b.status === "cancelled") return;
    wavesSeen.add(b.wave);
    const pts = b.route.nodes.map(n => nodes[n]).filter(Boolean);
    if (pts.length < 2) return;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join("");
    const dash = b.status === "departed" ? "" : `stroke-dasharray=".18 .12"`;
    html += `<path class="route-line" d="${d}" stroke="${waveColor(b.wave)}"
      stroke-width="${b.status === "departed" ? .09 : .06}" ${dash}/>`;
  });
  document.getElementById("waveLegend").innerHTML = [...wavesSeen].sort().map(w =>
    `<span><i class="line-leg" style="border-top-color:${waveColor(w)}"></i>第${w}波</span>`
  ).join("");

  // 网格节点
  S.grids.forEach(g => {
    const n = waiting[g.code] || 0;
    const fill = g.threatened ? "#ff7a3d" : "#4c6b8f";
    html += `<circle class="node-circle" cx="${g.x}" cy="${g.y}" r=".28" fill="${fill}"/>`;
    if (g.threatened)
      html += `<text class="fire-icon" x="${g.x}" y="${g.y - .42}">🔥</text>`;
    html += `<text class="node-label" x="${g.x}" y="${g.y + .52}">${esc(g.code)} ${esc(g.name)}</text>`;
    if (n) html += `<text class="node-sub" x="${g.x}" y="${g.y + .78}">待撤离 ${n} 户</text>`;
  });

  // 避难点
  S.shelters.forEach(s => {
    const cap = S.capacity[s.code];
    const suspended = s.status !== "active";
    const fill = suspended ? "#6b7280" : "#4cc38a";
    html += `<rect class="shelter-rect" x="${s.x - .26}" y="${s.y - .26}"
      width=".52" height=".52" rx=".08" fill="${fill}"/>`;
    html += `<text class="node-label" x="${s.x}" y="${s.y - .42}">${esc(s.code)} ${esc(s.name)}</text>`;
    const sub = suspended ? "已暂停接收"
      : `床 ${cap.committed}用/${cap.reserved}留/${cap.remaining}余`;
    html += `<text class="node-sub" x="${s.x}" y="${s.y + .52}">${esc(sub)}</text>`;
  });

  svg.innerHTML = html;
  svg.querySelectorAll(".road").forEach(el => {
    el.addEventListener("click", async () => {
      const code = el.dataset.road;
      const road = S.roads.find(r => r.code === code);
      const type = road.status === "open" ? "road_close" : "road_open";
      try {
        await api("/api/events", { type, code });
        toast(`道路 ${code} 已${type === "road_close" ? "封闭，仅撤回未出发批次" : "恢复，可继续编排"}`, "ok");
        refresh();
      } catch (e) { toast(e.message, "err"); }
    });
  });
}

// ---------------------------------------------------------------- 侧栏

function renderCapacity() {
  const box = document.getElementById("capList");
  box.innerHTML = Object.values(S.capacity).map(c => {
    const used = c.committed + c.reserved;
    const statusTag = c.status === "active"
      ? '<span class="tag ok">接收中</span>'
      : '<span class="tag bad">已暂停</span>';
    const flags = [c.accessible ? "♿" : "", c.pets_allowed ? "🐾" : ""]
      .filter(Boolean).join(" ");
    return `<div class="cap-row">
      <div>${esc(c.name)} ${statusTag} <span class="muted">${flags}</span></div>
      <div class="cap-bar">
        <div class="cap-committed" style="width:${100 * c.committed / c.beds}%"></div>
        <div class="cap-reserved" style="width:${100 * c.reserved / c.beds}%"></div>
        <div class="cap-free"></div>
      </div>
      <div class="muted">总 ${c.beds} ｜ 已承诺 ${c.committed} ｜ 已预留 ${c.reserved} ｜ 剩余 ${c.remaining}</div>
    </div>`;
  }).join("");
}

function renderVehicles() {
  const box = document.getElementById("vehList");
  const label = { available: ["ok", "可用"], down: ["bad", "故障"], departed: ["warn", "已离场"] };
  box.innerHTML = S.vehicles.map(v => {
    const [cls, name] = label[v.status] || ["", v.status];
    return `<div class="cap-row">🚐 ${esc(v.name)} <span class="muted">(${esc(v.code)})</span>
      ${v.accessible ? '♿' : ""} ${v.seats}座
      <span class="tag ${cls}">${name}</span></div>`;
  }).join("");
}

function renderSelectors() {
  const fill = (id, opts, current) => {
    const el = document.getElementById(id);
    if (el.dataset.built === "1") {
      el.value = current || el.value; return;
    }
    el.innerHTML = opts;
    el.dataset.built = "1";
  };
  fill("roadSel", S.roads.map(r =>
    `<option value="${esc(r.code)}">${esc(r.code)} ${esc(r.a)}↔${esc(r.b)}（${r.status === "open" ? "开放" : "封闭"}）</option>`).join(""));
  fill("vehSel", S.vehicles.map(v =>
    `<option value="${esc(v.code)}">${esc(v.code)} ${esc(v.name)}（${v.status}）</option>`).join(""));
  fill("shelSel", S.shelters.map(s =>
    `<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}（${s.status === "active" ? "接收" : "暂停"}）</option>`).join(""));
  fill("gridSel", S.grids.map(g =>
    `<option value="${esc(g.code)}">${esc(g.code)} ${esc(g.name)}（${g.threatened ? "受威胁" : "安全"}）</option>`).join(""));
}

// ---------------------------------------------------------------- 批次页

function batchChip(b) {
  const v = S.vehicles.find(x => x.code === b.vehicle) || {};
  const sh = S.shelters.find(x => x.code === b.shelter) || {};
  const hhs = b.households.map(ref => {
    const h = S.households.find(x => x.ref === ref) || {};
    return `${esc(h.name || ref)}(${h.members}人${h.mobility ? "♿" : ""}${h.pets ? "🐾" + h.pets : ""})`;
  }).join("、");
  const btn = b.status === "scheduled"
    ? `<button class="mini publish" onclick="depart('${esc(b.id)}')">确认车辆离场</button>`
    : b.status === "departed"
      ? `<span class="pill departed">✓ 已离场·已承诺</span>`
      : `<span class="pill cancelled">已撤回</span>`;
  return `<div class="batch-chip ${b.status}">
    <div><b>${esc(v.name || b.vehicle)}</b> <span class="muted">${esc(b.vehicle)}${v.accessible ? " ♿" : ""}</span></div>
    <div>${esc(b.grid)} → ${esc(sh.name || b.shelter)}</div>
    <div class="route">经 ${b.route.edges.map(esc).join(" → ")}</div>
    <div class="muted">${hhs || "（无家庭，已撤回待重排）"}</div>
    <div style="margin-top:6px">${btn}
      <span class="muted">${b.departed_at ? fmtTime(b.departed_at) : ""}</span></div>
  </div>`;
}

function renderBatches() {
  const area = document.getElementById("batchArea");
  const active = S.batches.filter(b => b.status !== "cancelled");
  const departed = active.filter(b => b.status === "departed");
  const scheduled = active.filter(b => b.status === "scheduled");

  let html = "";
  if (departed.length) {
    html += `<div class="commit-note">已离场 ${departed.length} 个批次为<b>不可变承诺</b>：
      家庭不会被改派，目的地床位持续占用，车辆不再参与编排。</div>`;
    html += `<div class="wave-block"><div class="wave-title">🚍 已承诺（已离场）</div>
      ${departed.map(batchChip).join("")}</div>`;
  }
  if (scheduled.length) {
    const waves = {};
    scheduled.forEach(b => (waves[b.wave] = waves[b.wave] || []).push(b));
    Object.keys(waves).map(Number).sort((a, c) => a - c).forEach(w => {
      html += `<div class="wave-block">
        <div class="wave-title" style="color:${waveColor(w)}">第 ${w} 波 · ${waves[w].length} 车待发</div>
        ${waves[w].map(batchChip).join("")}</div>`;
    });
  }
  if (!active.length) {
    html = `<p class="muted">当前没有生效中的撤离令。请到「方案比较与发布」生成并发布方案。
      受威胁待安排家庭：<b>${S.pending_households.length}</b> 户。</p>`;
  }
  area.innerHTML = html;
}

window.depart = async (id) => {
  if (!confirm("确认该批次车辆已离场？\n离场后批次与床位占用立即成为承诺，不可再改派或释放。")) return;
  try {
    const r = await api(`/api/batches/${id}/depart`, {});
    toast(`批次已离场并锁定，撤回同车后续批次 ${r.released_for_replan.length} 户待重排`, "ok");
    refresh();
  } catch (e) { toast(e.message, "err"); }
};

// ---------------------------------------------------------------- 方案比较

let currentPlanId = null;

async function generatePlan() {
  try {
    const commander = document.getElementById("commander").value.trim();
    const p = await api("/api/plan", { name: `方案 ${new Date().toLocaleTimeString("zh-CN")}`, commander });
    showPlanDetail(p, true);
    toast(p.feasible ? `已生成可行方案：${p.batches.length} 个批次 / ${p.max_wave} 个波次`
      : "已生成方案，但存在无法安置的家庭", p.feasible ? "ok" : "err");
    switchTab("plans");
    refresh();
  } catch (e) { toast(e.message, "err"); }
}

async function renderPlansList() {
  const box = document.getElementById("planCompare");
  let plans;
  try { plans = await api("/api/plans"); } catch { return; }
  if (!plans.length) {
    box.innerHTML = `<p class="muted">尚无候选方案。点击右上角「生成候选方案」，系统将只对
      <b>受威胁网格中尚未出发</b>的家庭求解；已出发承诺保持不变。可多次生成后比较再发布。</p>`;
    return;
  }
  box.innerHTML = `<div class="plan-cards">${plans.map(p => {
    const s = p.summary;
    const stale = p.base_revision !== S.revision && p.status === "candidate";
    return `<div class="plan-card ${p.status === "published" ? "published" : ""} ${!s.feasible ? "infeasible" : ""}"
      onclick="openPlan('${esc(p.id)}')">
      <div class="row-between"><b>${esc(p.name)}</b>
        ${p.status === "published" ? '<span class="tag bad">已生效</span>'
          : stale ? '<span class="tag warn">已过期</span>'
          : '<span class="tag ok">可发布</span>'}</div>
      <div class="muted">${esc(p.commander || "未署名")} · ${fmtTime(p.created_at)}</div>
      <div class="metric"><span>状态</span><b>${s.feasible ? "可行" : "含不可安置户"}</b></div>
      <div class="metric"><span>批次 / 波次</span><b>${s.batch_count} / ${s.max_wave ?? "–"}</b></div>
      <div class="metric"><span>覆盖家庭</span><b>${s.totals.households_assigned} / ${s.totals.households_planned}</b></div>
      <div class="metric"><span>使用车辆</span><b>${s.totals.vehicles_used}</b></div>
      <div class="metric"><span>依据修订号</span><b>${p.base_revision}${stale ? "（旧）" : ""}</b></div>
    </div>`;
  }).join("")}</div>`;
}

window.openPlan = async (id) => {
  try {
    const p = await api(`/api/plans/${id}`);
    showPlanDetail({ ...p.payload, id: p.id, name: p.name, commander: p.commander,
      status: p.status, base_revision: p.base_revision }, false);
  } catch (e) { toast(e.message, "err"); }
};

function showPlanDetail(p, justCreated) {
  currentPlanId = p.id;
  document.getElementById("planDetailCard").hidden = false;
  document.getElementById("planDetailTitle").textContent =
    `方案详情 — ${p.name || p.id}（修订号 ${p.base_revision ?? "?"}）`;
  document.getElementById("publishBtn").disabled =
    p.status === "published" || !p.feasible;
  document.getElementById("publishMsg").innerHTML = p.status === "published"
    ? '<div class="commit-note">该方案为当前<b>已生效</b>撤离令。</div>' : "";

  let html = `<div class="metric"><span>可行性</span><b>${p.feasible ? "✅ 全部家庭可撤离" : "❌ 存在无法安置家庭"}</b></div>
    <div class="metric"><span>批次数 / 出发波次</span><b>${p.batches.length} / ${p.max_wave ?? "—"}</b></div>`;

  if (p.batches.length) {
    const waves = {};
    p.batches.forEach(b => (waves[b.wave] = waves[b.wave] || []).push(b));
    html += Object.keys(waves).map(Number).sort((a, c) => a - c).map(w =>
      `<div class="wave-block"><div class="wave-title" style="color:${waveColor(w)}">第 ${w} 波</div>
      ${waves[w].map(b => `<div class="batch-chip scheduled">
        <div><b>${esc(b.vehicle_name)}</b> <span class="muted">${esc(b.vehicle)}</span></div>
        <div>${esc(b.grid)} → ${esc(b.shelter_name)}</div>
        <div class="route">经 ${b.route_edges.map(esc).join(" → ")}（${b.distance}）</div>
        <div class="muted">座位 ${b.seats_used}/${b.seats_total} ｜
          ${b.households.map(h => esc(h.name || h.ref)).join("、")}</div>
      </div>`).join("")}</div>`).join("");
  }

  if (p.unassigned.length) {
    html += `<div class="unassigned-box"><h4>⚠ 无法安置的 ${p.unassigned.length} 户及具体约束</h4>` +
      p.unassigned.map(u => `<div><b>${esc(u.name || u.ref)}</b>（${esc(u.grid)}，${u.members}人${u.mobility ? "，行动辅助" : ""}${u.pets ? "，宠物" + u.pets : ""}）
        ${u.reasons.map(r => `<div class="reason">• ${esc(r)}</div>`).join("")}</div>`).join("") +
      `<div class="muted" style="margin-top:6px">资源恢复（道路重开、车辆修复、避难点恢复、床位释放）后可再次生成方案继续编排。</div></div>`;
  }

  html += `<h3>发布后床位预测</h3><div>` + Object.values(p.capacity).map(c =>
    `<div class="cap-row">${esc(c.name)}：
      <span class="muted">总${c.beds} · 已承诺${c.committed} · 本方案预留${c.reserved} · 余${c.remaining}</span></div>`
  ).join("") + `</div>`;

  document.getElementById("planDetail").innerHTML = html;
}

document.getElementById("publishBtn").addEventListener("click", async () => {
  if (!currentPlanId) return;
  const commander = document.getElementById("commander").value.trim();
  try {
    const r = await api(`/api/plans/${currentPlanId}/publish`, { commander });
    document.getElementById("publishMsg").innerHTML =
      `<div class="commit-note">✅ 分区撤离令已发布（${r.batches} 个批次）。
        旧的未出发批次已被替换；已出发批次与床位占用不变。修订号 ${r.revision}。</div>`;
    toast("撤离令已生效", "ok");
    refresh();
    renderPlansList();
  } catch (e) {
    document.getElementById("publishMsg").innerHTML =
      `<div class="unassigned-box"><h4>发布被拒绝</h4><div class="reason">${esc(e.message)}</div>
      <div class="muted">这是乐观并发保护：已有其他指挥员发布，或路况/资源在此期间变化。
      请重新生成方案、比较后再发布。</div></div>`;
    toast(e.message, "err");
  }
});
document.getElementById("closeDetail").addEventListener("click",
  () => { document.getElementById("planDetailCard").hidden = true; });

// ---------------------------------------------------------------- 住户

function renderHouseholds() {
  const gridSel = document.getElementById("hGrid");
  if (gridSel.dataset.built !== "1") {
    gridSel.innerHTML = S.grids.map(g =>
      `<option value="${esc(g.code)}">${esc(g.code)} ${esc(g.name)}</option>`).join("");
    gridSel.dataset.built = "1";
  }
  const stLabel = {
    unplanned: ['<span class="tag warn">待安排</span>', ""],
    scheduled: ['<span class="tag ok">已排批次</span>', ""],
    departed: ['<span class="tag bad">已撤离·承诺</span>', ""],
  };
  const rowsHtml = S.households.map(h => {
    const st = S.household_state[h.ref] || { status: "unplanned" };
    const g = S.grids.find(x => x.code === h.grid) || {};
    return `<tr>
      <td>${esc(h.ref)}</td><td>${esc(h.name)}</td>
      <td>${esc(h.grid)} ${esc(g.name || "")} ${g.threatened ? "🔥" : ""}</td>
      <td>${h.members}</td>
      <td>${h.mobility ? "♿ 需要" : "—"}</td>
      <td>${h.pets || "—"}</td>
      <td>${esc(h.contact)}</td>
      <td>${(stLabel[st.status] || [""])[0]} ${st.shelter ? `<span class="muted">→${esc(st.shelter)} 波次${st.wave}</span>` : ""}</td>
    </tr>`;
  }).join("");
  document.getElementById("hhTable").innerHTML =
    `<tr><th>户号</th><th>称呼</th><th>网格</th><th>人数</th><th>行动辅助</th><th>宠物</th><th>联络</th><th>状态</th></tr>${rowsHtml}`;
}

document.getElementById("hAdd").addEventListener("click", async () => {
  const body = {
    ref: document.getElementById("hRef").value.trim(),
    name: document.getElementById("hName").value.trim(),
    grid: document.getElementById("hGrid").value,
    members: Number(document.getElementById("hMembers").value),
    mobility: document.getElementById("hMobility").checked ? 1 : 0,
    pets: Number(document.getElementById("hPets").value),
    contact: document.getElementById("hContact").value.trim(),
  };
  if (!body.ref) return toast("户号必填", "err");
  try {
    const r = await api("/api/households", body);
    toast(r.skipped_duplicates ? `户号已存在，跳过重复登记（新增 ${r.inserted}）`
      : `已登记（修订号 ${r.revision}）`, r.skipped_duplicates ? "" : "ok");
    ["hRef", "hName", "hContact"].forEach(i => document.getElementById(i).value = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 资源导入

const TEMPLATES = {
  grids: `[\n  {"code":"G7","name":"东岭","x":2.0,"y":4.6}\n]`,
  roads: `[\n  {"code":"r12","a":"G3","b":"G5","length":2.6,"capacity":1}\n]`,
  shelters: `[\n  {"code":"S5","name":"南山中心","x":6.0,"y":4.8,"beds":50,"accessible":1,"pets_allowed":1}\n]`,
  vehicles: `[\n  {"code":"V6","name":"中巴-06","seats":10,"accessible":0}\n]`,
};
document.getElementById("importKind").addEventListener("change", (e) => {
  document.getElementById("importJson").value = TEMPLATES[e.target.value];
});
document.getElementById("importJson").value = TEMPLATES.grids;
document.getElementById("importHelp").innerHTML =
  "字段说明：<br>• 网格：code,name,x,y<br>• 道路：code,a,b,length,capacity(每批次可过车辆数)<br>" +
  "• 避难点：code,name,x,y,beds,accessible,pets_allowed<br>• 车辆：code,name,seats,accessible<br>" +
  "同 code 重复导入执行覆盖更新，不会产生重复网格/道路/床位/车辆。";

document.getElementById("importBtn").addEventListener("click", async () => {
  const kind = document.getElementById("importKind").value;
  let items;
  try { items = JSON.parse(document.getElementById("importJson").value); }
  catch { return toast("JSON 格式错误", "err"); }
  try {
    const r = await api(`/api/import/${kind}`, items);
    toast(`已导入 ${r.imported} 条 ${kind}（修订号 ${r.revision}）`, "ok");
    document.querySelectorAll("#roadSel,#vehSel,#shelSel,#gridSel").forEach(el => el.dataset.built = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 事件

const bindEvent = (btnId, selId, type, okMsg) => {
  document.getElementById(btnId).addEventListener("click", async () => {
    const code = document.getElementById(selId).value;
    try {
      const r = await api("/api/events", { type, code });
      toast(okMsg(code) + `（撤回未出发 ${r.released_for_replan.length} 户待重排；已出发不受影响）`, "ok");
      document.querySelectorAll("#roadSel,#vehSel,#shelSel,#gridSel").forEach(el => el.dataset.built = "");
      refresh();
    } catch (e) { toast(e.message, "err"); }
  });
};
bindEvent("roadCloseBtn", "roadSel", "road_close", c => `道路 ${c} 已封闭`);
bindEvent("roadOpenBtn", "roadSel", "road_open", c => `道路 ${c} 已恢复，可继续编排`);
bindEvent("vehDownBtn", "vehSel", "vehicle_down", c => `车辆 ${c} 已故障`);
bindEvent("vehRecoverBtn", "vehSel", "vehicle_recovered", c => `车辆 ${c} 已恢复`);
bindEvent("shelSuspendBtn", "shelSel", "shelter_suspend", c => `避难点 ${c} 暂停接收`);
bindEvent("shelResumeBtn", "shelSel", "shelter_resume", c => `避难点 ${c} 恢复接收`);
bindEvent("fireClearBtn", "gridSel", "fire_clear", c => `网格 ${c} 解除威胁`);
document.getElementById("fireBtn").addEventListener("click", async () => {
  const code = document.getElementById("gridSel").value;
  const closeRoads = prompt("火线扩大，可同时封闭道路（输入道路编号，逗号分隔；留空不封路）", "");
  if (closeRoads === null) return;
  const body = { type: "fire_expand", code,
    close_roads: closeRoads.split(",").map(s => s.trim()).filter(Boolean) };
  try {
    const r = await api("/api/events", body);
    toast(`火线扩大至 ${code}；新增受威胁家庭将进入待编排，撤回未出发 ${r.released_for_replan.length} 户`, "ok");
    document.querySelectorAll("#roadSel,#gridSel").forEach(el => el.dataset.built = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 日志

function renderLog() {
  document.getElementById("logList").innerHTML = S.events.slice().reverse().map(e => {
    let detail = "";
    try { detail = JSON.stringify(JSON.parse(e.detail), null, 0); } catch { detail = e.detail; }
    return `<div class="logline"><span class="ts">${fmtTime(e.ts)}</span>
      <span class="kind">${esc(e.kind)}</span><span class="muted">r${e.revision}</span> ${esc(detail)}</div>`;
  }).join("");
}

document.getElementById("resetBtn").addEventListener("click", async () => {
  if (!confirm("重置将清空全部命令、批次与登记并恢复演示数据，确定？")) return;
  await api("/api/reset", {});
  toast("已重置为演示数据", "ok");
  refresh();
});

// ---------------------------------------------------------------- 框架

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach(p =>
    p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "plans") renderPlansList();
  if (name === "sandbox") { renderSandbox(); loadDiff(); }
  if (!Ctx.isBranch) Ctx._lastLiveTab = name;
}
document.querySelectorAll(".tab").forEach(t =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));
document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("planBtn").addEventListener("click", generatePlan);
document.getElementById("replanBtn").addEventListener("click", generatePlan);

// ---------------------------------------------------------------- 推演沙盒

let selectedBranchId = null;

const FIELD_LABEL = {
  "roads.status": { text: "道路状态", fmt: v => v === "closed" ? "封闭" : "开放" },
  "roads.capacity": { text: "道路容量", fmt: v => v },
  "shelters.status": { text: "避难点状态", fmt: v => v === "active" ? "接收中" : "已暂停" },
  "shelters.beds": { text: "床位数", fmt: v => v },
  "vehicles.status": { text: "车辆状态", fmt: v => ({ available: "可用", down: "故障", departed: "已离场" }[v] || v) },
  "grids.threatened": { text: "火线威胁", fmt: v => v ? "受威胁" : "安全" },
};
const STATUS_LABEL = { unplanned: "待安排", scheduled: "已排批次", departed: "已撤离", absent: "—" };

async function renderSandbox() {
  try { branchesCache = await api("/api/branches"); } catch { return; }
  // 正处于某分支上下文时，沙盒页默认选中该分支
  if (Ctx.isBranch && !branchesCache.some(b => b.id === selectedBranchId)) {
    selectedBranchId = Ctx.get().id;
  }
  const box = document.getElementById("branchList");
  if (!branchesCache.length) {
    box.innerHTML = `<p class="muted">尚无推演分支。输入名称后点击上方按钮，系统将把<b>当前实战态势</b>
      完整复制到隔离分支（含路网、运力、收容资源与已承诺状态），随后可在分支内连续模拟而不污染实战。</p>`;
  } else {
    box.innerHTML = `<div class="plan-cards">${branchesCache.map(b => {
      const sel = b.id === selectedBranchId;
      const tag = b.status === "discarded"
        ? '<span class="tag bad">已废弃</span>'
        : '<span class="tag ok">推演中</span>';
      return `<div class="plan-card ${sel ? "selected-branch" : ""}" onclick="selectBranch('${esc(b.id)}')">
        <div class="row-between"><b>🧪 ${esc(b.name)}</b>${tag}</div>
        <div class="muted">${esc(b.id)}</div>
        <div class="metric"><span>创建基线（实战修订号）</span><b>r${b.base_live_revision}</b></div>
        <div class="metric"><span>上次提升至实战修订号</span><b>r${b.baseline_revision}</b></div>
        <div class="muted">创建于 ${fmtTime(b.created_at)}${b.discarded_at ? " · 废弃于 " + fmtTime(b.discarded_at) : ""}</div>
      </div>`;
    }).join("")}</div>`;
  }
  // 上下文若指向的分支已不在列表则不动
  const diffCard = document.getElementById("diffCard");
  if (selectedBranchId) {
    diffCard.hidden = false;
    const b = branchesCache.find(x => x.id === selectedBranchId);
    if (b) document.getElementById("diffTitle").textContent = `分支 vs 实战 — ${b.name}`;
  } else {
    diffCard.hidden = true;
  }
}

window.selectBranch = (id) => {
  selectedBranchId = id;
  renderSandbox();
  loadDiff();
};

document.getElementById("createBranchBtn").addEventListener("click", async () => {
  const name = document.getElementById("newBranchName").value.trim() || `推演 ${new Date().toLocaleString("zh-CN", { hour12: false })}`;
  try {
    const b = await api("/api/branches", { name, idem_key: "create_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8) });
    selectedBranchId = b.id;
    document.getElementById("newBranchName").value = "";
    toast(b.idempotent_replay ? "该分支已存在（幂等返回）" : "已从当前实战态势创建隔离推演分支", "ok");
    await renderSandbox();
    loadDiff();
  } catch (e) { toast(e.message, "err"); }
});

document.getElementById("enterBranchBtn").addEventListener("click", () => {
  const b = branchesCache.find(x => x.id === selectedBranchId);
  if (!b) return;
  if (b.status === "discarded") return toast("分支已废弃，不能进入推演", "err");
  enterBranch(b.id, b.name, b.base_live_revision, b.status);
});

document.getElementById("discardBtn").addEventListener("click", async () => {
  if (!selectedBranchId) return;
  if (!confirm("废弃该推演分支？\n分支文件与审计记录会保留（仍可只读查看差异），但不能再模拟或提升。")) return;
  try {
    const r = await api(`/api/branches/${selectedBranchId}/discard`, {});
    toast(r.idempotent_replay ? "分支此前已废弃（幂等）" : "分支已废弃，实战数据未受影响", "ok");
    if (Ctx.isBranch && Ctx.get().id === selectedBranchId) exitToLive();
    await renderSandbox();
    loadDiff();
  } catch (e) { toast(e.message, "err"); }
});

document.getElementById("promoteBtn").addEventListener("click", async () => {
  if (!selectedBranchId || !diffCache) return;
  if (!confirm("提升将做三方冲突检查：\n只应用未与实战冲突的资源改动；任一冲突或基线过期都会整体拒绝。\n推演产生的命令、离场记录与已承诺占用不会带入实战。继续？")) return;
  try {
    const r = await api(`/api/branches/${selectedBranchId}/promote`, {
      expected_live_revision: diffCache.live_revision,
      promote_id: "promote_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
    });
    document.getElementById("promoteMsg").innerHTML =
      `<div class="commit-note">✅ 已提升 ${r.applied_count} 个资源字段改动（实战修订号 r${r.revision}）。
        推演命令/离场/承诺占用未迁移。${r.skipped_commitments.length ? "已忽略推演离场承诺：" +
        r.skipped_commitments.map(c => c.code).join("、") : ""}</div>`;
    toast(`提升成功：${r.applied_count} 个字段`, "ok");
    await renderSandbox();
    loadDiff();
    refresh();
  } catch (e) {
    const p = e.payload || {};
    let detail = "";
    if (p.code === "conflict") {
      detail = `<div class="reason">冲突对象：${esc((p.conflict_objects || []).join("、"))}</div>` +
        (p.conflicts || []).map(c =>
          `<div class="reason">• ${esc(c.table)} <b>${esc(c.code)}</b>.${esc(c.field)}：
            基线=${esc(c.base)} ｜ 实战=${esc(c.live)} ｜ 推演=${esc(c.branch)}</div>`).join("") +
        `<div class="muted">已整体拒绝，实战数据未发生任何变化；另有 ${p.pending_clean_changes ?? 0} 个干净改动未应用。</div>`;
    } else if (p.code === "stale_baseline") {
      detail = `<div class="reason">${esc(e.message)}</div>
        <div class="muted">差异视图基于 r${p.expected_live_revision}，实战已推进到 r${p.current_live_revision}。</div>`;
    } else {
      detail = `<div class="reason">${esc(e.message)}</div>`;
    }
    document.getElementById("promoteMsg").innerHTML =
      `<div class="unassigned-box"><h4>提升被拒绝（整体回滚）</h4>${detail}</div>`;
    toast("提升被拒绝：" + e.message, "err");
    loadDiff();
  }
});

async function loadDiff() {
  if (!selectedBranchId) return;
  try {
    diffCache = await api(`/api/branches/${selectedBranchId}/diff`);
    renderDiff(diffCache);
  } catch (e) {
    document.getElementById("promoteMsg").innerHTML =
      `<div class="unassigned-box">${esc(e.message)}</div>`;
  }
}

function renderDiff(d) {
  const discarded = d.branch.status === "discarded";
  document.getElementById("promoteBtn").disabled = discarded;
  document.getElementById("enterBranchBtn").disabled = discarded;

  document.getElementById("diffMeta").innerHTML = `
    <span class="tag ok">实战 r${d.live_revision}</span>
    <span class="tag warn">分支 r${d.branch_revision}</span>
    <span class="muted">创建基线 r${d.base_live_revision} ｜ 上次提升至 r${d.last_promoted_revision ?? "—"}</span>
    <span class="muted">｜ 实战：命令 ${d.live_summary.orders} · 已离场 ${d.live_summary.departed} · 待安排 ${d.live_summary.pending}</span>
    <span class="muted">｜ 推演：命令 ${d.branch_summary.orders} · 已离场 ${d.branch_summary.departed} · 待安排 ${d.branch_summary.pending}</span>`;

  // 资源字段差异
  const fieldRows = d.resource_fields.map(f => {
    const lab = FIELD_LABEL[`${f.table}.${f.field}`] || { text: f.field, fmt: x => x };
    const cls = { conflict: "cell-conflict", branch_only: "cell-clean",
      live_only: "cell-live", same_change: "cell-same", commitment: "cell-commit" }[f.status] || "";
    const tag = { conflict: '<span class="tag bad">冲突</span>',
      branch_only: '<span class="tag ok">可提升</span>',
      live_only: '<span class="tag warn">仅实战</span>',
      same_change: '<span class="tag ok">两边一致</span>',
      commitment: '<span class="tag warn">推演承诺·不迁移</span>' }[f.status] || "";
    return `<tr class="${cls}">
      <td>${esc(f.table)}</td><td><b>${esc(f.code)}</b></td><td>${esc(lab.text)}</td>
      <td>${esc(lab.fmt(f.base))}</td>
      <td>${esc(lab.fmt(f.live))}</td>
      <td>${esc(lab.fmt(f.branch))}</td><td>${tag}</td></tr>`;
  }).join("");
  document.getElementById("fieldDiffTable").innerHTML =
    `<tr><th>对象类型</th><th>编号</th><th>字段</th><th>创建基线</th><th>实战</th><th>推演</th><th>判定</th></tr>` +
    (fieldRows || `<tr><td colspan="7" class="muted">资源字段无差异</td></tr>`);

  // 人员去向
  renderPeopleTable("peopleLiveTable", d.people, "live");
  renderPeopleTable("peopleBranchTable", d.people, "branch");
  // 床位
  renderCapTable("capLiveTable", d.shelters, "live");
  renderCapTable("capBranchTable", d.shelters, "branch");
  // 受阻原因
  renderBlockers("blockerLive", d.blockers, "live_reasons");
  renderBlockers("blockerBranch", d.blockers, "branch_reasons");

  // 审计
  api(`/api/branches/${selectedBranchId}/audit`).then(a => {
    document.getElementById("branchAudit").innerHTML = a.length
      ? a.slice(0, 30).map(e => `<div class="logline"><span class="ts">${fmtTime(e.ts)}</span>
          <span class="kind">${esc(e.action)}</span> <span class="muted">${esc(e.detail)}</span></div>`).join("")
      : '<p class="muted">无审计记录</p>';
  }).catch(() => {});
}

function renderPeopleTable(id, people, side) {
  const rowsHtml = people.filter(p => p.differs).map(p => {
    const s = p[side];
    return `<tr>
      <td>${esc(p.ref)}</td>
      <td>${esc(STATUS_LABEL[s.status] || s.status || "—")}</td>
      <td>${esc(s.shelter || "—")}</td>
      <td>${s.wave ?? "—"}</td></tr>`;
  }).join("");
  document.getElementById(id).innerHTML =
    `<tr><th>户号</th><th>状态</th><th>去向避难点</th><th>波次</th></tr>` +
    (rowsHtml || `<tr><td colspan="4" class="muted">与另一侧无差异</td></tr>`);
}

function renderCapTable(id, shelters, side) {
  const rowsHtml = shelters.filter(x => x.differs).map(x => {
    const s = x[side];
    if (!s) return `<tr><td>${esc(x.code)}</td><td colspan="5" class="cell-conflict">该侧不存在</td></tr>`;
    return `<tr>
      <td><b>${esc(x.code)}</b> ${esc(x.name)}</td>
      <td>${s.beds}</td><td>${s.committed}</td><td>${s.reserved}</td>
      <td class="${s.remaining < 0 ? "cell-conflict" : ""}">${s.remaining}</td>
      <td>${s.status === "active" ? "接收中" : "已暂停"}</td></tr>`;
  }).join("");
  document.getElementById(id).innerHTML =
    `<tr><th>避难点</th><th>总床位</th><th>已承诺</th><th>已预留</th><th>剩余</th><th>状态</th></tr>` +
    (rowsHtml || `<tr><td colspan="6" class="muted">与另一侧无差异</td></tr>`);
}

function renderBlockers(id, blockers, key) {
  const box = document.getElementById(id);
  const items = blockers.filter(b => b[key].length);
  box.innerHTML = items.length
    ? items.map(b => `<div class="batch-chip scheduled" style="display:block;min-width:0">
        <b>${esc(b.name || b.ref)}</b> <span class="muted">${esc(b.ref)}</span>
        ${b[key].map(r => `<div class="reason">• ${esc(r)}</div>`).join("")}</div>`).join("")
    : '<p class="muted">无受阻家庭（或尚未生成方案）</p>';
}

refresh();
setInterval(refresh, 6000);

/* 离线车队回执中继 —— 控制台前端（原生 JS，无构建） */
const $ = (id) => document.getElementById(id);
const API = "/relay/api";
let timer = null;
let selectedBatch = null;
let currentUser = localStorage.getItem("relay_user") || "";
let enforceApprovals = true;
let accountsCache = [];

const ROLE_LABEL = { observe: "观察", dispatch: "调度", archive_review: "归档审查" };
const AP_STATUS = {
  pending: "待复核", approved: "已批准", rejected: "已拒绝",
  invalid: "已失效", executed: "已执行", execute_failed: "执行被业务拒绝",
};

// ------------------------------------------------------------ 通用
function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 3200);
  t.classList.remove("hidden");
}

async function api(path, opts = {}) {
  const headers = opts.body ? { "Content-Type": "application/json" } : {};
  if (currentUser) headers["X-Relay-User"] = currentUser;
  const resp = await fetch(API + path, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await resp.json(); } catch { /* ignore */ }
  if (!resp.ok) {
    const err = new Error((data && data.error) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function loadAccountsIntoSelector() {
  try {
    const d = await api("/accounts");
    accountsCache = d.accounts;
    const sel = $("userSelect");
    const prev = currentUser;
    sel.innerHTML = '<option value="">（未登录）</option>' +
      d.accounts.map((a) => `<option value="${esc(a.username)}"${
        a.username === prev ? " selected" : ""}${a.active ? "" : " disabled"}>
        ${esc(a.display_name || a.username)}${a.active ? "" : "（已停用）"}</option>`)
        .join("");
    sel.value = prev;
  } catch { /* 账号接口需要登录/令牌时静默 */ }
}

$("userSelect").addEventListener("change", async () => {
  currentUser = $("userSelect").value;
  localStorage.setItem("relay_user", currentUser);
  await refreshAllViews();
});

async function myRoles() {
  if (!currentUser) return new Set();
  const a = accountsCache.find((x) => x.username === currentUser);
  return new Set(a ? a.role_list : []);
}

function actionNeedsRole(action) {
  return ({ package_import: "dispatch", manual_retry: "dispatch",
            ack_terminal: "dispatch", batch_seal: "archive_review" })[action];
}

function fmtTs(ts) {
  if (ts === null || ts === undefined) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}
function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const STATE_BADGE = {
  buffered: "缓冲", queued: "待转发", dispatching: "转发中",
  retry_wait: "等待重试", forwarded: "已转发", rejected_terminal: "终态拒绝",
};
function stateBadge(s) {
  return `<span class="badge ${s}">${STATE_BADGE[s] || s}</span>`;
}

// ------------------------------------------------------------ Tab 切换
document.querySelectorAll(".topbar nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".topbar nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((s) => s.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "terminal") loadTerminal();
    if (btn.dataset.tab === "retries") loadQueue();
    if (btn.dataset.tab === "archive") loadArchiveTab();
    if (btn.dataset.tab === "audit") loadAudit();
    if (btn.dataset.tab === "approvals") loadApprovalsTab();
    if (btn.dataset.tab === "accounts") loadAccounts();
    if (btn.dataset.tab === "stub") loadStub();
  });
});

// ---- 敏感操作统一走双人复核 ----
async function submitSensitive(action, params) {
  if (!currentUser) {
    toast("请先在右上角选择值班账号", "err");
    throw new Error("未登录");
  }
  const roles = await myRoles();
  const need = actionNeedsRole(action);
  if (!roles.has(need)) {
    toast(`该操作需要「${ROLE_LABEL[need]}」权限，当前账号没有`, "err");
    throw new Error("无权限");
  }
  const d = await api("/approvals", { method: "POST", body: { action, ...params } });
  if (d.idempotent_replay && d.status === "pending") {
    toast(`已有相同待批事项 ${d.id}（幂等），等待另一账号复核`);
  } else if (d.status === "pending") {
    toast(`待批事项 ${d.id} 已创建，请切换另一账号在「双人复核」页批准`, "ok");
  } else {
    toast(`事项 ${d.id} 当前状态：${AP_STATUS[d.status] || d.status}`);
  }
  return d;
}

// ------------------------------------------------------------ 批次总览
async function loadHealth() {
  try {
    const h = await api("/health");
    enforceApprovals = h.enforce_approvals !== false;
    $("health").textContent =
      `适配器: ${h.adapter} | 派发 ${h.forwarder.dispatched} 成功 ${h.forwarder.acked} ` +
      `重试 ${h.forwarder.retries} 终态 ${h.forwarder.terminal} | 启动回收 ${h.startup.recovered_leases}`;
    $("health").classList.remove("bad");
    if (h.adapter === "stub") {
      document.querySelector('[data-tab="stub"]').classList.remove("hidden");
    }
  } catch {
    $("health").textContent = "中继不可达";
    $("health").classList.add("bad");
  }
  await loadAccountsIntoSelector();
  if (currentUser) {
    try {
      const todo = await api("/approvals/todo");
      const badge = $("todoBadge");
      if (todo.total) {
        badge.textContent = todo.total;
        badge.classList.remove("hidden");
      } else badge.classList.add("hidden");
    } catch { /* ignore */ }
  } else $("todoBadge").classList.add("hidden");
}

async function loadBatches() {
  const d = await api("/batches");
  const box = $("batchCards");
  if (!d.batches.length) {
    box.innerHTML = `<p class="muted">还没有导入执行包，请到「执行包导入」页导入。</p>`;
  } else {
    box.innerHTML = d.batches.map((b) => {
      const c = b.counts;
      const gapTxt = b.gaps.length
        ? `<div class="gapline">序号缺口：${b.gaps.join(", ")}（游标待 ${b.cursor}）</div>`
        : (b.status === "collecting" || b.status === "forwarding")
          ? `<div class="muted" style="font-size:12px;margin-top:6px">无缺口，游标 @${b.cursor}</div>`
          : "";
      const blocker = b.blocker
        ? `<div class="gapline">⛔ 游标冻结于序号 ${b.blocker.seq}（${esc(b.blocker.result_code)}），后续不得越过</div>`
        : "";
      const metrics = b.archived
        ? `<div class="metrics">
            <span class="metric">已归档回执 <b>${b.archive ? b.archive.receipt_count : 0}</b></span>
            <span class="metric">回收空间 <b>${fmtBytes(b.archive ? b.archive.freed_bytes : 0)}</b></span>
          </div>
          <div class="muted" style="font-size:12px;margin-top:6px">🗄️ 已封存为只读归档
            ${b.archive ? "· 封存于 " + fmtTs(b.archive.sealed_at) : ""}</div>`
        : `<div class="metrics">
            <span class="metric">已转发 <b>${c.forwarded}</b></span>
            <span class="metric">待转发 <b>${c.queued}</b></span>
            <span class="metric">缓冲 <b>${c.buffered}</b></span>
            <span class="metric">重试中 <b>${c.retry_wait + c.dispatching}</b></span>
            <span class="metric">终态拒绝 <b class="${c.rejected_terminal ? "err-text" : ""}">${c.rejected_terminal}</b></span>
          </div>
          ${gapTxt}${blocker}`;
      return `<div class="card" data-ref="${esc(b.batch_ref)}">
        <h4>${esc(b.batch_ref)} <span class="badge ${b.status}">${statusText(b.status)}</span></h4>
        <div class="sub">${esc(b.batch_summary)}</div>
        <div class="sub mono">hash ${esc(b.content_hash.slice(0, 16))}… · 导入于 ${fmtTs(b.imported_at)}</div>
        ${metrics}
      </div>`;
    }).join("");
    box.querySelectorAll(".card").forEach((card) =>
      card.addEventListener("click", () => loadBatchDetail(card.dataset.ref)));
  }
  if (selectedBatch) await loadBatchDetail(selectedBatch);
}

function statusText(s) {
  return { idle: "等待回执", collecting: "收集中", forwarding: "转发中",
           blocked_terminal: "终态阻塞", all_forwarded: "全部转发完成",
           archived: "已封存(只读)" }[s] || s;
}

async function loadBatchDetail(ref) {
  selectedBatch = ref;
  const b = await api("/batches/" + encodeURIComponent(ref));
  const c = b.counts;
  const blocked = b.blocker
    ? `<div class="blocker-box">⛔ <b>终态拒绝（游标冻结）</b>：序号 ${b.blocker.seq}
        回执 ${esc(b.blocker.receipt_no)} 被主系统拒绝（<code>${esc(b.blocker.result_code)}</code>）。
        原因：${esc(b.blocker.result_detail)}。后续回执无法越过该项；
        只有确认主系统终态解除后，到「失败重试」页人工确认重投。</div>` : "";
  const buffered = b.buffered_items.length
    ? `<div><b>缓冲项（持久等待缺口）：</b> ${b.buffered_items.map((x) =>
        `<span class="badge buffered">#${x.seq} ${esc(x.receipt_no)}</span>`).join(" ")}</div>` : "";
  const archivedBox = b.archived
    ? `<div class="archive-notice">🗄️ <b>该批次已封存为只读归档</b>
        ${b.archive ? `（封存于 ${fmtTs(b.archive.sealed_at)}，回收 ${fmtBytes(b.archive.freed_bytes)}）` : ""}。
        校验摘要 <code>${esc(b.archive ? b.archive.digest.slice(0, 24) : "")}…</code>；
        以下明细来自归档快照，不再进入转发队列。可去「归档封存」页下载完整归档。</div>` : "";
  const auditSection = (b.archived && b.audit && b.audit.length)
    ? `<h3>原审计时间线（随归档保留）</h3>
       <table class="tbl"><thead><tr><th>时间</th><th>事件</th><th>序号</th>
         <th>回执号</th><th>操作者</th><th>细节</th></tr></thead>
       <tbody>${b.audit.map((e) => `<tr>
         <td class="mono">${fmtTs(e.ts)}</td><td>${esc(e.kind)}</td>
         <td>${e.seq ?? ""}</td><td>${esc(e.receipt_no)}</td><td>${esc(e.actor)}</td>
         <td class="mono" style="max-width:360px;word-break:break-all">${esc(e.detail)}</td>
       </tr>`).join("")}</tbody></table>` : "";
  const rows = b.receipts.map((r) => `<tr>
      <td>${r.seq}</td>
      <td>${esc(r.receipt_no)}</td>
      <td>${r.type_label}</td>
      <td>${stateBadge(r.state)}</td>
      <td>${r.attempts}</td>
      <td class="${r.state === "forwarded" ? "ok-text" : r.last_error || r.result_detail ? "err-text" : ""}">
        ${esc(r.last_error || r.result_detail || "")}
        ${r.result_code && r.result_code !== "ack" ? `<div class="muted mono">${esc(r.result_code)}</div>` : ""}</td>
      <td>${r.forwarded_at ? "✅ " + fmtTs(r.forwarded_at) : ""}
          ${r.rejected_at ? "⛔ " + fmtTs(r.rejected_at) : ""}
          ${r.next_retry_at ? "↻ " + fmtTs(r.next_retry_at) : ""}</td>
    </tr>`).join("");
  $("batchDetail").classList.remove("hidden");
  $("batchDetail").innerHTML = `
    <div class="row between">
      <h2 style="margin:0">批次 ${esc(b.batch_ref)}</h2>
      <button onclick="document.getElementById('batchDetail').classList.add('hidden')">关闭</button>
    </div>
    <p class="hint">${esc(b.batch_summary)} · 游标 next_seq=<b>${b.cursor}</b>
      · 最大已见序号 ${b.max_seen ?? "—"} · 缺口 [${b.gaps.join(", ") || "无"}]</p>
    ${archivedBox}${blocked}${buffered}
    <table class="tbl"><thead><tr>
      <th>序号</th><th>回执号</th><th>类型</th><th>状态</th><th>尝试</th>
      <th>转发结果 / 失败原因</th><th>时间</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="muted">尚无回执</td></tr>`}</tbody></table>
    ${auditSection}`;
}

// ------------------------------------------------------------ 执行包导入
const SAMPLE_PKG = {
  batch_ref: "B-DEMO-1",
  package_id: "PKG-2026-1002",
  batch_summary: "波次1 · 车辆V-07 · 网格G3 → 避难点S1（共 4 户 9 人）",
  seq_base: 1,
  issued_at: "2026-10-02T07:30:00Z",
  items: [
    { household_ref: "H-101", members: 3, seats_taken: 3 },
    { household_ref: "H-102", members: 2, seats_taken: 2 },
    { household_ref: "H-103", members: 2, seats_taken: 2 },
    { household_ref: "H-104", members: 2, seats_taken: 3, pets: 1 },
  ],
  vehicle: { code: "V-07", seats: 11 },
  destination: { code: "S1", name: "城北体育馆避难点" },
};
$("loadSampleBtn").onclick = () => {
  $("pkgJson").value = JSON.stringify(SAMPLE_PKG, null, 2);
};
$("importBtn").onclick = async () => {
  let pkg;
  try { pkg = JSON.parse($("pkgJson").value); }
  catch { $("importResult").textContent = "JSON 解析失败"; return; }
  try {
    let r;
    if (enforceApprovals) {
      r = await submitSensitive("package_import", { package: pkg });
      $("importResult").textContent = JSON.stringify(r, null, 2)
        + "\n\n⚠️ 导入未执行：已形成待批事项，请切换另一调度账号到「双人复核」页批准。";
    } else {
      r = await api("/packages/import", { method: "POST", body: { package: pkg } });
      $("importResult").textContent = JSON.stringify(r, null, 2);
      toast(r.replay ? "相同包再次导入：返回原结果（幂等）" : "执行包已不可变存档", "ok");
    }
    loadBatches();
  } catch (e) {
    if (e.data) $("importResult").textContent = JSON.stringify(e.data, null, 2);
    toast(e.message, "err");
  }
};

// ------------------------------------------------------------ 现场终端
async function loadTerminal() {
  const d = await api("/terminal/manifest");
  $("terminalRows").innerHTML = d.batches.map((b) => `<tr>
    <td><b>${esc(b.batch_ref)}</b>${b.archived ? ' <span class="badge archived">已封存</span>' : ""}</td>
    <td>${esc(b.batch_summary)}</td>
    <td class="mono">${esc(b.content_hash.slice(0, 20))}…</td>
    <td>${b.forwarded_receipts}</td>
    <td>${fmtTs(b.imported_at)}</td>
    <td><button data-ref="${esc(b.batch_ref)}">下载</button></td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">暂无批次</td></tr>`;
  $("terminalRows").querySelectorAll("button").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const pkg = await api("/terminal/packages/" + encodeURIComponent(btn.dataset.ref));
      $("terminalPkg").classList.remove("hidden");
      $("terminalPkg").textContent = JSON.stringify(pkg, null, 2);
    }));
}

// ------------------------------------------------------------ 回执上传
function makeReceipt(seq, no, type, summary) {
  return { seq, receipt_no: no, type, batch_summary: summary,
           at: new Date().toISOString(), note: `现场回执 ${no}` };
}
$("outOfOrderSampleBtn").onclick = () => {
  $("rcptJson").value = JSON.stringify({
    batch_ref: "B-DEMO-1",
    batch_summary: SAMPLE_PKG.batch_summary,
    receipts: [
      makeReceipt(1, "R-1001", "departed", SAMPLE_PKG.batch_summary),
      makeReceipt(3, "R-1003", "arrived", SAMPLE_PKG.batch_summary),
    ],
  }, null, 2);
};
$("uploadBtn").onclick = async () => {
  let body;
  try { body = JSON.parse($("rcptJson").value); }
  catch { $("uploadResult").textContent = "JSON 解析失败"; return; }
  try {
    const r = await api("/receipts/batch", { method: "POST", body });
    $("uploadResult").textContent = JSON.stringify(r, null, 2);
    toast(`已处理 ${r.count} 条回执`, "ok");
    loadBatches();
  } catch (e) {
    $("uploadResult").textContent = JSON.stringify(e.data || { error: e.message }, null, 2);
    toast(e.message, "err");
  }
};

// ------------------------------------------------------------ 重试
async function loadQueue() {
  const d = await api("/queue");
  $("queueRows").innerHTML = d.pending.map((r) => {
    let action = "";
    if (r.state === "retry_wait" || r.state === "dispatching") {
      action = `<button data-act="retry" data-ref="${esc(r.batch_ref)}" data-seq="${r.seq}">立即重试</button>`;
    } else if (r.state === "rejected_terminal") {
      action = `<button class="warn" data-act="ack" data-ref="${esc(r.batch_ref)}" data-seq="${r.seq}">
        确认终态已解除并重投</button>`;
    } else {
      action = `<span class="muted">—</span>`;
    }
    return `<tr>
      <td>${esc(r.batch_ref)}</td><td>${r.seq}</td><td>${esc(r.receipt_no)}</td>
      <td>${r.receipt_type}</td><td>${stateBadge(r.state)}</td><td>${r.attempts}</td>
      <td class="err-text">${esc(r.last_error || r.result_code || "")}</td>
      <td>${r.next_retry_at ? fmtTs(r.next_retry_at) : ""}</td>
      <td>${action}</td></tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">队列已清空</td></tr>`;

  $("queueRows").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const note = prompt("操作备注（将进入审计与复核记录）：", "操作员现场确认") || "";
      try {
        if (enforceApprovals) {
          const action = btn.dataset.act === "retry" ? "manual_retry" : "ack_terminal";
          const params = btn.dataset.act === "retry"
            ? { batch_ref: btn.dataset.ref, seq: Number(btn.dataset.seq), note }
            : { batch_ref: btn.dataset.ref, seq: Number(btn.dataset.seq), note };
          await submitSensitive(action, params);
        } else if (btn.dataset.act === "retry") {
          await api("/retries", { method: "POST",
            body: { batch_ref: btn.dataset.ref, seq: Number(btn.dataset.seq), note } });
          toast("已提交", "ok");
        } else {
          await api(`/batches/${encodeURIComponent(btn.dataset.ref)}/receipts/${btn.dataset.seq}/ack-terminal`,
            { method: "POST", body: { note } });
          toast("已提交", "ok");
        }
        loadQueue(); loadBatches();
      } catch (e) { if (e.data || e.status) toast(e.message, "err"); }
    });
  });
}
$("queueRefresh").onclick = loadQueue;

// ------------------------------------------------------------ 归档封存
async function loadArchiveTab() {
  const [s, a] = await Promise.all([api("/admin/storage"), api("/archives")]);
  const t = s.totals;
  $("storageTotals").innerHTML =
    `<span class="metric">热库占用 <b>${fmtBytes(t.hot_bytes)}</b></span>
     <span class="metric">可封存批次 <b>${t.sealable}</b></span>
     <span class="metric">已封存 <b>${t.archived}</b></span>
     <span class="metric">累计回收 <b>${fmtBytes(t.freed_bytes)}</b></span>
     <span class="metric">归档体积 <b>${fmtBytes(t.archive_bytes)}</b></span>`;
  $("storageRows").innerHTML = s.batches.map((b) => {
    if (b.archived) {
      return `<tr><td><b>${esc(b.batch_ref)}</b></td>
        <td colspan="3" class="muted">热库明细已迁入只读归档</td>
        <td><span class="badge archived">已封存</span></td>
        <td class="muted">已回收 ${fmtBytes(b.archive ? b.archive.freed_bytes : 0)}</td></tr>`;
    }
    const eligible = b.sealable
      ? `<span class="badge forwarded">✓ 可封存</span>`
      : `<div class="err-text" style="font-size:12px">${b.reasons.map((r) => esc(r.message)).join("<br>")}</div>`;
    const action = b.sealable
      ? `<button class="warn" data-ref="${esc(b.batch_ref)}">执行封存</button>`
      : `<span class="muted">—</span>`;
    return `<tr><td><b>${esc(b.batch_ref)}</b></td>
      <td>${b.receipt_count} 条 / ${fmtBytes(b.receipt_bytes)}</td>
      <td>${b.audit_count} 行 / ${fmtBytes(b.audit_bytes)}</td>
      <td><b>${fmtBytes(b.hot_bytes)}</b></td>
      <td>${eligible}</td><td>${action}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">暂无批次</td></tr>`;
  $("storageRows").querySelectorAll("button[data-ref]").forEach((btn) =>
    btn.addEventListener("click", () => sealBatch(btn.dataset.ref)));

  $("archiveRows").innerHTML = a.archives.map((x) => `<tr>
    <td><b>${esc(x.batch_ref)}</b></td>
    <td class="mono" title="${esc(x.digest)}">${esc(x.digest.slice(0, 20))}…</td>
    <td>${x.receipt_count}</td><td>${x.audit_count}</td>
    <td>${fmtBytes(x.freed_bytes)}</td><td>${fmtTs(x.sealed_at)}</td>
    <td><button data-view="${esc(x.batch_ref)}">校验并查看</button>
      <a class="btnlike" href="/relay/api/archives/${encodeURIComponent(x.batch_ref)}/download">下载</a></td>
  </tr>`).join("") || `<tr><td colspan="7" class="muted">暂无归档</td></tr>`;
  $("archiveRows").querySelectorAll("button[data-view]").forEach((btn) =>
    btn.addEventListener("click", () => viewArchive(btn.dataset.view)));
}

async function sealBatch(ref) {
  const note = prompt(`确认申请封存批次 ${ref}？\n` +
    "封存为敏感指令，将形成待批事项，由另一归档审查账号批准。可附备注：",
    "定期空间回收");
  if (note === null) return;
  try {
    if (enforceApprovals) {
      const r = await submitSensitive("batch_seal", { batch_ref: ref, note });
      if (r.status === "pending") {
        document.querySelector('[data-tab="approvals"]').click();
      }
    } else {
      const r = await api(`/batches/${encodeURIComponent(ref)}/seal`,
        { method: "POST", body: { note } });
      toast(r.replay ? "批次此前已封存，返回第一次结果"
                     : `已封存 ${ref}，回收 ${fmtBytes(r.freed_bytes)}`, "ok");
    }
    loadArchiveTab(); loadBatches();
  } catch (e) {
    const reasons = e.data && e.data.reasons
      ? "\n" + e.data.reasons.map((x) => "· " + x.message).join("\n") : "";
    alert(e.message + reasons);
  }
}

async function viewArchive(ref) {
  let d;
  try { d = await api("/archives/" + encodeURIComponent(ref)); }
  catch (e) {
    $("archiveDetail").classList.remove("hidden");
    $("archiveDetail").innerHTML =
      `<div class="blocker-box">⛔ <b>归档校验失败，已拒绝恢复</b>：${esc(e.message)}<br>
        <span class="mono">${esc(JSON.stringify(e.data || {}))}</span></div>`;
    return;
  }
  const rows = d.receipts.map((r) => `<tr>
    <td>${r.seq}</td><td>${esc(r.receipt_no)}</td><td>${r.type_label}</td>
    <td>${stateBadge(r.state)}</td><td>${r.attempts}</td>
    <td class="mono">${esc((r.content_hash || "").slice(0, 16))}…</td>
    <td>${r.forwarded_at ? fmtTs(r.forwarded_at) : ""}</td></tr>`).join("");
  const auditRows = d.audit.map((e) => `<tr>
    <td class="mono">${fmtTs(e.ts)}</td><td>${esc(e.kind)}</td>
    <td>${e.seq ?? ""}</td><td>${esc(e.receipt_no)}</td><td>${esc(e.actor)}</td>
    <td class="mono" style="max-width:360px;word-break:break-all">${esc(e.detail)}</td></tr>`).join("");
  $("archiveDetail").classList.remove("hidden");
  $("archiveDetail").innerHTML = `
    <div class="row between">
      <h3 style="margin:0">归档 ${esc(d.batch_ref)}（只读详情）</h3>
      <button onclick="document.getElementById('archiveDetail').classList.add('hidden')">关闭</button>
    </div>
    <div class="ok-box">✅ <b>校验通过</b>：摘要 <code>${esc(d.digest.slice(0, 24))}…</code>
      · 封存于 ${fmtTs(d.sealed_at)}（${esc(d.sealed_by)}）
      · 回执 ${d.manifest.receipt_count} 条 · 原审计 ${d.manifest.audit_count} 行</div>
    <p class="hint">${esc(d.manifest.batch_summary)} · 游标封存于 next_seq=${d.manifest.cursor_at_seal}
      · 执行包哈希 <code>${esc(d.manifest.package_hash.slice(0, 16))}…</code>
      ${d.note ? "· 备注：" + esc(d.note) : ""}</p>
    <h3>回执明细（归档快照）</h3>
    <table class="tbl"><thead><tr><th>序号</th><th>回执号</th><th>类型</th><th>状态</th>
      <th>尝试</th><th>内容哈希</th><th>转发完成</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="muted">无回执</td></tr>`}</tbody></table>
    <h3>原审计时间线</h3>
    <table class="tbl"><thead><tr><th>时间</th><th>事件</th><th>序号</th><th>回执号</th>
      <th>操作者</th><th>细节</th></tr></thead>
      <tbody>${auditRows || `<tr><td colspan="6" class="muted">无审计记录</td></tr>`}</tbody></table>`;
}
$("storageRefresh").onclick = loadArchiveTab;

// ------------------------------------------------------------ 审计
async function loadAudit() {
  const [a, cf] = await Promise.all([api("/audit"), api("/conflicts")]);
  $("auditRows").innerHTML = a.events.map((e) => `<tr>
    <td class="mono">${fmtTs(e.ts)}</td><td>${esc(e.kind)}</td>
    <td>${esc(e.batch_ref)}</td><td>${e.seq ?? ""}</td>
    <td>${esc(e.receipt_no)}</td><td>${esc(e.actor)}</td>
    <td class="mono" style="max-width:420px;word-break:break-all">${esc(e.detail)}</td></tr>`).join("");
  $("conflictRows").innerHTML = cf.conflicts.map((e) => `<tr>
    <td class="mono">${fmtTs(e.ts)}</td><td><span class="badge rejected_terminal">${esc(e.reason)}</span></td>
    <td>${esc(e.batch_ref)}</td><td>${e.seq ?? ""}</td>
    <td>${esc(e.receipt_no)}</td>
    <td class="mono" style="max-width:420px;word-break:break-all">${esc(e.detail)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">暂无冲突记录</td></tr>`;
}
$("auditRefresh").onclick = loadAudit;

// ------------------------------------------------------------ 桩适配器
async function loadStub() {
  let d;
  try { d = await api("/admin/state"); }
  catch (e) { $("stubPanel").innerHTML = `<p class="err-text">${esc(e.message)}</p>`; return; }
  if (d.adapter !== "stub") {
    $("stubPanel").innerHTML = `<p class="muted">当前适配器为 ${esc(d.adapter)}（设置
      RELAY_CALLBACK_ADAPTER=stub 可启用离线演示桩）。</p>`;
    return;
  }
  const calls = (d.stub.calls || []).slice().reverse().slice(0, 30);
  $("stubPanel").innerHTML = `
    <p>当前规则：<code>${esc(JSON.stringify(d.stub.rules || {}))}</code></p>
    <textarea id="stubRules" rows="6">${esc(JSON.stringify(d.stub.rules || {}, null, 2))}</textarea>
    <div class="row gap">
      <button class="primary" id="stubApply">应用规则并唤醒转发</button>
      <button id="stubReset">清空调用记录</button>
    </div>
    <h3>最近 30 次适配器调用（含重放，可据此验证恰好一次）</h3>
    <table class="tbl"><thead><tr><th>#</th><th>幂等键</th><th>批次</th><th>序号</th>
      <th>回执号</th><th>命中规则</th></tr></thead>
      <tbody>${calls.map((c, i) => `<tr><td>${calls.length - i}</td>
        <td class="mono">${esc(c.idempotency_key)}</td><td>${esc(c.batch_ref)}</td>
        <td>${c.seq}</td><td>${esc(c.receipt_no)}</td><td>${esc(c.rule)}</td></tr>`).join("")}
      </tbody></table>`;
  $("stubApply").onclick = async () => {
    try {
      const rules = JSON.parse($("stubRules").value);
      await api("/admin/stub", { method: "POST", body: { rules } });
      toast("规则已应用", "ok"); loadStub();
    } catch (e) { toast(e.message, "err"); }
  };
  $("stubReset").onclick = async () => {
    await api("/admin/stub", { method: "POST", body: { reset_calls: true } });
    loadStub();
  };
}

// ------------------------------------------------------------ 双人复核
function apBadge(status) {
  const cls = ({ pending: "queued", executed: "forwarded", rejected: "rejected_terminal",
                 invalid: "buffered", approved: "dispatching",
                 execute_failed: "rejected_terminal" })[status] || "";
  return `<span class="badge ${cls}">${AP_STATUS[status] || status}</span>`;
}

function apCard(a, showActions) {
  const p = a.payload_obj || {};
  const target = a.action === "package_import"
    ? esc(p.package && p.package.batch_ref || a.resource_ref)
    : `${esc(a.resource_ref)}${p.seq != null ? " #" + p.seq : ""}`;
  const expired = a.status === "pending" && a.expires_at < Date.now() / 1000;
  const reason = a.status === "invalid"
    ? `<div class="err-text" style="font-size:12px">失效原因：${esc(a.invalid_reason)}
       （${fmtTs(a.decided_at)}）</div>` : "";
  const execErr = a.status === "execute_failed" && a.execution_result_obj
    ? `<div class="err-text" style="font-size:12px">业务拒绝：
       ${esc(a.execution_result_obj.code)} — ${esc(a.execution_result_obj.error)}</div>` : "";
  let actions = "";
  if (showActions && a.can_approve) {
    actions = `<div class="row gap" style="margin-top:8px">
      <button data-decide="approve" data-id="${a.id}" class="primary">批准并执行</button>
      <button data-decide="reject" data-id="${a.id}">拒绝</button></div>`;
  }
  return `<div class="card ap-card status-${a.status}">
    <h4>${esc(a.action_label)} · ${target} ${apBadge(a.status)}
      <span class="muted mono" style="font-size:11px">${esc(a.id)}</span></h4>
    <div class="sub">申请人：<b>${esc(a.requested_by)}</b> · ${fmtTs(a.requested_at)}
      ${a.decided_by ? ` · 决策人：<b>${esc(a.decided_by)}</b> · ${fmtTs(a.decided_at)}` : ""}
      ${expired ? '<span class="err-text">· 已超时</span>' : ""}</div>
    <div class="sub mono" style="font-size:11px">资源版本 ${esc((a.resource_version || "").slice(0, 16))}…
      · 请求指纹 ${esc((a.request_hash || "").slice(0, 16))}…</div>
    ${reason}${execErr}${actions}</div>`;
}

async function loadApprovalsTab() {
  const [todo, inv, all, dec] = await Promise.all([
    currentUser ? api("/approvals/todo") : Promise.resolve({ my_requests: [], awaiting_my_review: [] }),
    api("/approvals?status=invalid").catch(() => ({ approvals: [] })),
    api("/approvals").catch(() => ({ approvals: [] })),
    api("/approval-decisions").catch(() => ({ decisions: [] })),
  ]);

  $("todoMine").innerHTML = todo.my_requests.length
    ? todo.my_requests.map((a) => apCard(a, false)).join("")
    : `<p class="muted">没有我发起的未决事项。</p>`;
  $("todoReview").innerHTML = todo.awaiting_my_review.length
    ? `<h4 style="margin:10px 0 6px">待我复核（非本人申请）</h4>` +
      todo.awaiting_my_review.map((a) => apCard(a, true)).join("")
    : "";
  $("invalidList").innerHTML = inv.approvals.length
    ? inv.approvals.map((a) => apCard(a, false)).join("")
    : `<p class="muted">暂无已失效事项。</p>`;
  $("approvalAll").innerHTML = all.approvals.length
    ? `<div class="cards">${all.approvals.map((a) => apCard(a, false)).join("")}</div>`
    : `<p class="muted">暂无事项。</p>`;
  $("decisionRows").innerHTML = dec.decisions.map((e) => `<tr>
    <td class="mono">${fmtTs(e.ts)}</td>
    <td class="mono">${esc(e.approval_id)}</td>
    <td>${esc(e.action)}</td>
    <td><span class="badge ${e.kind === "executed" ? "forwarded"
      : e.kind === "invalidated" || e.kind === "execute_failed" ? "rejected_terminal"
      : e.kind === "rejected" ? "rejected_terminal" : "queued"}">${esc(e.kind)}</span></td>
    <td>${esc(e.actor)}</td>
    <td class="mono" style="max-width:480px;word-break:break-all">${esc(e.detail)}</td>
  </tr>`).join("");

  document.querySelectorAll("button[data-decide]").forEach((btn) =>
    btn.addEventListener("click", () => decideApproval(btn.dataset.id, btn.dataset.decide)));
  // 动作联动：导入时显示包输入框，其余显示批次/序号
  syncApForm();
}

function syncApForm() {
  const action = $("apAction").value;
  const isImport = action === "package_import";
  $("apPkgJson").parentNode.style.display = isImport ? "" : "none";
  $("apBatch").style.display = isImport ? "none" : "";
  $("apSeq").style.display = action === "manual_retry" || action === "ack_terminal" ? "" : "none";
}
$("apAction").addEventListener("change", syncApForm);

$("apSubmit").onclick = async () => {
  const action = $("apAction").value;
  const note = $("apNote").value;
  try {
    let params;
    if (action === "package_import") {
      params = { package: JSON.parse($("apPkgJson").value) };
    } else {
      params = { batch_ref: $("apBatch").value.trim(), note };
      const seq = $("apSeq").value.trim();
      if (seq) params.seq = Number(seq);
      if (action === "ack_terminal" && seq === "") throw new Error("终态解除重投必须填 seq");
    }
    const d = await submitSensitive(action, params);
    $("apResult").textContent = JSON.stringify(d, null, 2);
    await loadApprovalsTab();
  } catch (e) {
    $("apResult").textContent = (e.data ? JSON.stringify(e.data, null, 2) : "")
      + `\n${e.message}`;
    toast(e.message, "err");
  }
};

async function decideApproval(id, decision) {
  const note = prompt(decision === "approve"
    ? `批准事项 ${id}？批准后立即在原有业务校验下执行。复核备注：`
    : `拒绝事项 ${id}？拒绝理由：`, "双人复核");
  if (note === null) return;
  try {
    const r = await api(`/approvals/${encodeURIComponent(id)}/decide`,
      { method: "POST", body: { decision, note } });
    toast(decision === "approve"
      ? (r.status === "executed" ? "已批准并执行：" + (r.message || "")
         : r.status === "execute_failed" ? "已批准，但业务校验拒绝执行"
         : "决策已记录：" + r.status)
      : "已拒绝", r.status === "executed" ? "ok" : "");
    $("apResult").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    alert(`${e.message}\n${e.data ? JSON.stringify(e.data, null, 2) : ""}`);
  }
  await loadApprovalsTab();
  loadBatches(); loadHealth();
}
$("apRefresh").onclick = loadApprovalsTab;

// ------------------------------------------------------------ 值班账号
async function loadAccounts() {
  const d = await api("/accounts");
  $("accountRows").innerHTML = d.accounts.map((a) => `<tr>
    <td><b>${esc(a.username)}</b></td>
    <td>${esc(a.display_name)}</td>
    <td>${a.role_list.map((r) =>
      `<span class="badge ${r === "observe" ? "queued" : r === "dispatch"
        ? "dispatching" : "retry_wait"}">${ROLE_LABEL[r] || r}</span>`).join(" ")}</td>
    <td>${a.active ? '<span class="badge forwarded">启用</span>'
                    : '<span class="badge rejected_terminal">已停用</span>'}</td>
    <td>
      <button data-edit='${esc(JSON.stringify(a))}'>编辑</button>
      <button data-toggle="${esc(a.username)}" data-active="${a.active ? 0 : 1}">
        ${a.active ? "停用" : "启用"}</button>
    </td></tr>`).join("");
  $("accountRows").querySelectorAll("button[data-edit]").forEach((b) =>
    b.addEventListener("click", () => {
      const a = JSON.parse(b.dataset.edit);
      $("accUser").value = a.username;
      $("accName").value = a.display_name;
      $("roleObserve").checked = a.role_list.includes("observe");
      $("roleDispatch").checked = a.role_list.includes("dispatch");
      $("roleArchive").checked = a.role_list.includes("archive_review");
      $("accActive").checked = !!a.active;
    }));
  $("accountRows").querySelectorAll("button[data-toggle]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`确认${b.dataset.active === "1" ? "启用" : "停用"}账号 ${b.dataset.toggle}？`
        + (b.dataset.active === "0" ? "\n该账号名下未决事项将立即失效。" : ""))) return;
      try {
        await api(`/accounts/${encodeURIComponent(b.dataset.toggle)}/active`,
          { method: "POST", body: { active: b.dataset.active === "1" } });
        toast("账号状态已更新", "ok");
        await loadAccounts(); await loadAccountsIntoSelector();
      } catch (e) { toast(e.message, "err"); }
    }));
}

$("accSave").onclick = async () => {
  const roles = [];
  if ($("roleObserve").checked) roles.push("observe");
  if ($("roleDispatch").checked) roles.push("dispatch");
  if ($("roleArchive").checked) roles.push("archive_review");
  try {
    const r = await api("/accounts", { method: "POST", body: {
      username: $("accUser").value.trim(), display_name: $("accName").value.trim(),
      roles, active: $("accActive").checked } });
    toast(r.created ? "账号已创建" : `账号已调整；${(r.invalidated || []).length} 个未决事项失效`,
      "ok");
    await loadAccounts(); await loadAccountsIntoSelector();
  } catch (e) { toast(e.message, "err"); }
};
$("accRefresh").onclick = loadAccounts;

// ------------------------------------------------------------ 自动刷新
$("refreshBtn").onclick = () => { loadBatches(); loadHealth(); };
async function refreshAllViews() {
  await loadHealth();
  loadBatches();
  const active = document.querySelector(".tab.active");
  if (active) {
    if (active.id === "tab-retries") loadQueue();
    if (active.id === "tab-archive") loadArchiveTab();
    if (active.id === "tab-audit") loadAudit();
    if (active.id === "tab-approvals") loadApprovalsTab();
    if (active.id === "tab-accounts") loadAccounts();
  }
}
function armTimer() {
  clearInterval(timer);
  if ($("autoRefresh").checked) {
    timer = setInterval(() => {
      loadHealth();
      loadBatches();
      const active = document.querySelector(".tab.active");
      if (active && active.id === "tab-retries") loadQueue();
      if (active && active.id === "tab-archive") loadArchiveTab();
      if (active && active.id === "tab-audit") loadAudit();
      if (active && active.id === "tab-approvals") loadApprovalsTab();
    }, 2000);
  }
}
$("autoRefresh").onchange = armTimer;

loadHealth().then(loadBatches);
armTimer();
syncApForm();

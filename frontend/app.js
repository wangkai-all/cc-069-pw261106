/* 商品分类沿革工作台 — 原生 JS SPA（无依赖、无框架） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  treeDate: "2026-06-15",
  categories: [],
  products: [],
};

/* ---------------- HTTP ---------------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}
const get = (path) => api(path);
const post = (path, body) => api(path, { method: "POST", body });

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => el.classList.add("hidden"), 3200);
  el.classList.remove("hidden");
}

function fmtMoney(v) {
  return "¥" + Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function fmtQty(v) {
  return Number(v).toLocaleString("zh-CN");
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------- 主题 ---------------- */
function initTheme() {
  const select = $("#themeSelect");
  const saved = localStorage.getItem("wb-theme") || "auto";
  select.value = saved;
  applyTheme(saved);
  select.addEventListener("change", () => {
    localStorage.setItem("wb-theme", select.value);
    applyTheme(select.value);
    if ($("#view-compare").classList.contains("active")) loadCompare();
  });
}
function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
}
const cssVar = (name) => getComputedStyle(document.documentElement)
  .getPropertyValue(name).trim();

/* ---------------- Tabs ---------------- */
$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((t) => t.classList.remove("active"));
  $$(".view").forEach((v) => v.classList.remove("active"));
  tab.classList.add("active");
  $("#view-" + tab.dataset.view).classList.add("active");
  const loaders = {
    tree: loadTree, point: loadPointView, compare: loadCompare,
    reports: loadReports, timeline: loadTimelineView,
  };
  loaders[tab.dataset.view]?.();
}));

/* ---------------- 通用弹窗 ---------------- */
function openModal(title, bodyHtml, onOk) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalBackdrop").classList.remove("hidden");
  const ok = $("#modalOk");
  const cancel = $("#modalCancel");
  const close = () => $("#modalBackdrop").classList.add("hidden");
  cancel.onclick = close;
  ok.onclick = async () => {
    try {
      await onOk();
      close();
    } catch (err) {
      toast(err.message, true);
    }
  };
}
$("#modalBackdrop").addEventListener("click", (e) => {
  if (e.target.id === "modalBackdrop")
    $("#modalBackdrop").classList.add("hidden");
});

/* ================================================================
 * 视图 1：沿革树 + 拖拽
 * ================================================================ */
async function loadCategories() {
  state.categories = await get("/api/categories");
}
function catName(id) {
  return state.categories.find((c) => c.id === id)?.name || id;
}
/* 操作表单只允许选择当前仍有效的分类；已停用/合并节点仅供留档查看 */
function activeCategoryOptions() {
  return state.categories
    .filter((c) => !c.retired_at)
    .map((c) => `<option value="${esc(c.id)}">${esc(c.code)} · ${esc(c.name)}</option>`)
    .join("");
}

async function loadTree() {
  await loadCategories();
  const data = await get(`/api/tree?date=${state.treeDate}`);
  $("#treeDate").value = data.date;
  state.treeDate = data.date;
  const banner = $("#validationBanner");
  if (data.validation.length) {
    banner.className = "banner err";
    banner.textContent = "⚠ 检测到 " + data.validation.length +
      " 个时点的树结构问题：" + data.validation.map((p) => p.error).join("；");
  } else {
    banner.className = "banner ok";
    banner.textContent = "✓ 全历史时点校验通过：无循环、无同一时点多父级";
  }
  const canvas = $("#treeCanvas");
  canvas.innerHTML = "";
  data.tree.forEach((node) => canvas.appendChild(renderTreeNode(node)));
  if (data.archived?.length) {
    const arch = document.createElement("div");
    arch.className = "archived-box";
    arch.innerHTML = `<h3>已停用 / 已合并留档（该时点之后不再挂在树上，血缘可追溯）</h3>`;
    data.archived.forEach((a) => {
      const chip = document.createElement("span");
      chip.className = "archived-chip";
      chip.innerHTML =
        `<span class="node-code">${esc(a.code)}</span> ${esc(a.name)}` +
        `<span class="badge ${a.kind}">${a.kind === "merged" ? "已合并" : "已停用"}</span>` +
        `<span class="node-count">${a.retired_at} 起</span>`;
      arch.appendChild(chip);
    });
    canvas.appendChild(arch);
  }
}

function renderTreeNode(node) {
  const wrap = document.createElement("div");
  wrap.className = "tree-node";
  wrap.dataset.id = node.id;

  const row = document.createElement("div");
  row.className = "node-row";
  row.draggable = true;
  const badge = node.kind === "root" ? `<span class="badge root">根</span>`
    : node.kind === "retired" ? `<span class="badge retired">已停用</span>`
    : node.kind === "merged" ? `<span class="badge merged">已合并</span>` : "";
  row.innerHTML =
    `<span class="caret">${node.children.length ? "▾" : ""}</span>` +
    `<span class="node-code">${esc(node.code)}</span>` +
    `<span class="node-name">${esc(node.name)}</span>` +
    badge +
    `<span class="node-count">${node.product_count} 个商品</span>`;

  // 拖拽调整父级
  row.addEventListener("dragstart", (e) => {
    wrap.classList.add("dragging");
    e.dataTransfer.setData("text/plain", node.id);
    e.dataTransfer.effectAllowed = "move";
  });
  row.addEventListener("dragend", () => wrap.classList.remove("dragging"));
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    row.classList.add("drop-target");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
  row.addEventListener("drop", async (e) => {
    e.preventDefault();
    row.classList.remove("drop-target");
    const childId = e.dataTransfer.getData("text/plain");
    if (!childId || childId === node.id) return;
    await promptMoveCategory(childId, node.id);
  });
  row.addEventListener("click", () => {
    const kids = wrap.querySelector(".children");
    const caret = row.querySelector(".caret");
    if (kids) {
      const hidden = kids.style.display === "none";
      kids.style.display = hidden ? "" : "none";
      if (caret) caret.textContent = hidden ? "▾" : "▸";
    }
  });
  wrap.appendChild(row);

  if (node.children.length) {
    const kids = document.createElement("div");
    kids.className = "children";
    node.children.forEach((c) => kids.appendChild(renderTreeNode(c)));
    wrap.appendChild(kids);
  }
  return wrap;
}

function promptMoveCategory(childId, newParentId) {
  return new Promise((resolve) => {
    openModal("移动分类", `
      <p>将 <b>${esc(catName(childId))}</b> 移动到新父级
         <b>${esc(catName(newParentId))}</b> 下。</p>
      <label>生效日期</label>
      <input type="date" id="mEffDate" value="${state.treeDate}">
      <label><input type="checkbox" id="mBackfill"> 这是补录过去生效的调整</label>
      <label>调整原因（可选）</label>
      <textarea id="mReason" placeholder="例：组织架构调整"></textarea>
    `, async () => {
      await post("/api/categories/move", {
        child_id: childId,
        new_parent_id: newParentId,
        effective_date: $("#mEffDate").value,
        backfill: $("#mBackfill").checked,
        reason: $("#mReason").value || null,
      });
      toast("分类移动已记录，旧关系已留档");
      await loadTree();
      resolve();
    });
  });
}

$("#treeDate").addEventListener("change", (e) => {
  state.treeDate = e.target.value;
  loadTree();
});
$("#treeRefresh").addEventListener("click", loadTree);
$("#btnValidate").addEventListener("click", async () => {
  const r = await post("/api/validate", {});
  toast(r.ok ? "校验通过：全历史无循环、无多父级"
    : "发现 " + r.problems.length + " 个问题", !r.ok);
});

$("#btnNewCategory").addEventListener("click", () => {
  const options = activeCategoryOptions();
  openModal("新建分类", `
    <label>分类编码</label><input id="cCode" placeholder="如 BOOKS">
    <label>分类名称</label><input id="cName" placeholder="如 图书文娱">
    <label>父级分类</label><select id="cParent">${options}</select>
    <label>生效日期</label><input type="date" id="cDate" value="${state.treeDate}">
    <label>原因（可选）</label><textarea id="cReason"></textarea>
  `, async () => {
    await post("/api/categories/create", {
      code: $("#cCode").value.trim(),
      name: $("#cName").value.trim(),
      parent_id: $("#cParent").value,
      effective_date: $("#cDate").value,
      reason: $("#cReason").value || null,
    });
    toast("分类已创建");
    await loadTree();
  });
});

$("#btnSplit").addEventListener("click", async () => {
  await loadCategories();
  const options = activeCategoryOptions();
  openModal("拆分分类（≥2 个新分类）", `
    <label>待拆分分类</label><select id="sSource">${options}</select>
    <label>新分类 1：编码 / 名称</label>
    <input id="sCode1" placeholder="编码，如 PHONE">
    <input id="sName1" placeholder="名称，如 手机数码" style="margin-top:6px">
    <label>新分类 2：编码 / 名称</label>
    <input id="sCode2" placeholder="编码，如 SMART">
    <input id="sName2" placeholder="名称，如 智能设备" style="margin-top:6px">
    <label>生效日期</label><input type="date" id="sDate" value="${state.treeDate}">
    <label>原因</label><textarea id="sReason"></textarea>
    <p class="hint">拆分后该分类下的商品默认全部进入新分类 1，可在「时间点查询」中再逐个调整。</p>
  `, async () => {
    const source = $("#sSource").value;
    const products = (await get("/api/products"))
      .filter((p) => p.current_category_id === source).map((p) => p.id);
    await post("/api/categories/split", {
      source_category_id: source,
      targets: [
        { code: $("#sCode1").value.trim(), name: $("#sName1").value.trim(),
          parent_id: "cat_root", products },
        { code: $("#sCode2").value.trim(), name: $("#sName2").value.trim(),
          parent_id: "cat_root", products: [] },
      ],
      effective_date: $("#sDate").value,
      reason: $("#sReason").value || null,
    });
    toast("拆分完成，源分类已停用并保留血缘");
    await loadTree();
  });
});

$("#btnMerge").addEventListener("click", () => {
  const options = activeCategoryOptions();
  openModal("合并分类", `
    <label>源分类（多个，按住 Ctrl 多选，至少 2 个）</label>
    <select id="mSources" multiple size="5" style="width:100%">${options}</select>
    <label>并入目标分类</label><select id="mTarget">${options}</select>
    <label>生效日期</label><input type="date" id="mDate" value="${state.treeDate}">
    <label>原因</label><textarea id="mReason2"></textarea>
  `, async () => {
    const sources = [...$("#mSources").selectedOptions].map((o) => o.value);
    const target = $("#mTarget").value;
    if (sources.length < 2) throw new Error("至少选择两个源分类");
    if (sources.includes(target)) throw new Error("目标分类不能同时是源分类");
    await post("/api/categories/merge", {
      source_category_ids: sources,
      target_category_id: target,
      effective_date: $("#mDate").value,
      reason: $("#mReason2").value || null,
    });
    toast("合并完成，源分类已标记并保留血缘");
    await loadTree();
  });
});

/* ================================================================
 * 视图 2：时间点查询
 * ================================================================ */
async function loadPointView() {
  await loadCategories();
  state.products = await get("/api/products");
  const sel = $("#pointProduct");
  sel.innerHTML = `<option value="">全部商品</option>` +
    state.products.map((p) =>
      `<option value="${esc(p.id)}">${esc(p.sku)} · ${esc(p.name)}</option>`).join("");
  await runPointQuery();
}

async function runPointQuery() {
  const date = $("#pointDate").value;
  const pid = $("#pointProduct").value;
  const data = await get(`/api/point-in-time?date=${date}` +
    (pid ? `&product_id=${pid}` : ""));
  const tbody = $("#pointTable tbody");
  tbody.innerHTML = data.products.map((p) => `
    <tr>
      <td>${esc(p.sku)}</td>
      <td>${esc(p.name)}</td>
      <td>${p.category
        ? `<span class="node-code">${esc(p.category.code)}</span> ${esc(p.category.name)}`
        : `<span class="hint">未分类</span>`}</td>
    </tr>`).join("");
  if (!pid) {
    renderProductHistoryList(data.products[0]?.product_id);
  } else {
    renderProductHistoryList(pid);
  }
}

async function renderProductHistoryList(pid) {
  if (!pid) {
    $("#productHistory").innerHTML = `<p class="hint">请选择商品</p>`;
    return;
  }
  const data = await get(`/api/history?product_id=${pid}`);
  $("#productHistory").innerHTML = data.history.map((h) => `
    <div class="history-item">
      <div class="range">${h.effective_from} → ${h.effective_to || "至今"}</div>
      <div><b>${esc(h.category_code)} · ${esc(h.category_name)}</b></div>
      <div class="meta">${h.change_title
        ? `调整单 #${h.change_id} · ${esc(h.change_title)}` : "初始归属"}</div>
    </div>`).join("");
}

$("#pointQuery").addEventListener("click", runPointQuery);
$("#pointDate").addEventListener("change", runPointQuery);
$("#pointProduct").addEventListener("change", runPointQuery);

/* ================================================================
 * 视图 3：销量双口径对照（SVG 分组柱状图）
 * ================================================================ */
async function loadCompare() {
  const from = $("#cmpFrom").value;
  const to = $("#cmpTo").value;
  $("#cmpExport").href = `/api/compare/export?from=${from}&to=${to}`;
  const data = await get(`/api/compare?from=${from}&to=${to}`);

  // 统计卡片
  const t = data.totals;
  $("#cmpStats").innerHTML = [
    ["交易时口径总额", fmtMoney(t.pointwise_amount), ""],
    ["当前口径总额", fmtMoney(t.current_amount), ""],
    ["重分类商品数", String(t.moved_product_count), "个商品跨分类"],
    ["总额守恒差", fmtMoney(t.amount_delta),
      t.amount_delta === 0 ? "两口径总额一致 ✓" : ""],
  ].map(([label, value, sub]) => `
    <div class="stat-tile">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      ${sub ? `<div class="delta">${sub}</div>` : ""}
    </div>`).join("");

  renderGroupedBarChart(data.rows);
  renderCompareTables(data);
}

function renderCompareTables(data) {
  $("#cmpTable tbody").innerHTML = data.rows.map((r) => `
    <tr>
      <td><span class="node-code">${esc(r.category_code)}</span> ${esc(r.category_name)}</td>
      <td class="num">${fmtQty(r.pointwise_qty)}</td>
      <td class="num">${fmtQty(r.current_qty)}</td>
      <td class="num ${r.qty_delta > 0 ? "delta-up" : r.qty_delta < 0 ? "delta-down" : ""}">
        ${r.qty_delta > 0 ? "+" : ""}${fmtQty(r.qty_delta)}</td>
      <td class="num">${fmtMoney(r.pointwise_amount)}</td>
      <td class="num">${fmtMoney(r.current_amount)}</td>
      <td class="num ${r.amount_delta > 0 ? "delta-up" : r.amount_delta < 0 ? "delta-down" : ""}">
        ${r.amount_delta > 0 ? "+" : ""}${fmtMoney(r.amount_delta)}</td>
    </tr>`).join("");

  $("#cmpDetail tbody").innerHTML = data.detail.map((d) => `
    <tr style="${d.reclassified ? "" : "opacity:.62"}">
      <td>${esc(d.sku)}</td><td>${esc(d.product_name)}</td>
      <td>${esc(d.pointwise_category)}</td>
      <td>${d.reclassified
        ? `<b>${esc(d.current_category)}</b>` : esc(d.current_category)}</td>
      <td class="num">${fmtQty(d.qty)}</td>
      <td class="num">${fmtMoney(d.amount)}</td>
    </tr>`).join("");
}

/* 分组柱状图：每分类两根柱（交易时=蓝 slot1 / 当前=橙 slot2），
   遵循 24px 柱宽上限、4px 圆角数据端、2px 表面间隙、hairline 网格、
   每柱 hover 命中区 ≥24px，文本一律用文字色。 */
function renderGroupedBarChart(rows) {
  const host = $("#cmpChart");
  const nonzero = rows.filter((r) => r.pointwise_amount || r.current_amount);
  if (!nonzero.length) {
    host.innerHTML = `<p class="hint">所选区间无销售数据</p>`;
    return;
  }
  const c1 = cssVar("--series-1");
  const c2 = cssVar("--series-2");
  const ink = cssVar("--text-secondary");
  const muted = cssVar("--text-muted");
  const grid = cssVar("--gridline");
  const baseline = cssVar("--baseline");

  const W = Math.max(640, nonzero.length * 118);
  const H = 380;
  const padL = 64, padR = 24, padT = 18, padB = 84;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const maxVal = Math.max(...nonzero.flatMap((r) =>
    [r.pointwise_amount, r.current_amount]));
  const niceMax = niceCeil(maxVal);
  const y = (v) => padT + plotH - (v / niceMax) * plotH;

  const groupW = plotW / nonzero.length;
  const barW = Math.min(24, groupW / 4);
  const gap = 2; // 表面间隙

  const ticks = 4;
  let gridSvg = "";
  for (let i = 0; i <= ticks; i++) {
    const val = (niceMax / ticks) * i;
    const yy = y(val);
    gridSvg += `<line class="grid-line" x1="${padL}" x2="${W - padR}"
                  y1="${yy}" y2="${yy}" stroke="${grid}"/>`;
    gridSvg += `<text class="axis-label" x="${padL - 8}" y="${yy + 4}"
                  text-anchor="end" fill="${muted}">${compactMoney(val)}</text>`;
  }

  let bars = "";
  nonzero.forEach((r, i) => {
    const cx = padL + groupW * i + groupW / 2;
    const x1 = cx - barW - gap / 2;
    const x2 = cx + gap / 2;
    const label = r.category_name.length > 6
      ? r.category_name.slice(0, 5) + "…" : r.category_name;
    bars += barSvg(x1, y(r.pointwise_amount), barW,
      padT + plotH - y(r.pointwise_amount), c1, r, "pointwise");
    bars += barSvg(x2, y(r.current_amount), barW,
      padT + plotH - y(r.current_amount), c2, r, "current");
    bars += `<text class="bar-label" x="${cx}" y="${H - padB + 18}"
              text-anchor="middle" fill="${ink}">${esc(label)}</text>`;
    bars += `<text class="bar-label" x="${cx}" y="${H - padB + 34}"
              text-anchor="middle" fill="${muted}">${esc(r.category_code)}</text>`;
  });

  host.innerHTML = `
    <div class="legend">
      <span class="key"><span class="swatch" style="background:${c1}"></span>交易时分类口径</span>
      <span class="key"><span class="swatch" style="background:${c2}"></span>当前分类口径</span>
    </div>
    <svg width="${W}" height="${H}" role="img"
         aria-label="各分类在两种口径下的销售额分组柱状图">
      ${gridSvg}
      <line class="baseline-line" x1="${padL}" x2="${W - padR}"
            y1="${padT + plotH}" y2="${padT + plotH}" stroke="${baseline}"/>
      ${bars}
    </svg>
    <div id="chartTip" class="chart-tip hidden"></div>`;

  const tip = $("#chartTip");
  host.querySelectorAll("[data-tip]").forEach((hit) => {
    const show = (e) => {
      const d = JSON.parse(hit.dataset.tip);
      tip.innerHTML =
        `<div class="tip-key"><span class="tip-swatch"
            style="background:${d.color}"></span>${esc(d.caliber)}</div>
         <div class="tip-val">${fmtMoney(d.value)}</div>
         <div>${esc(d.cat)} · 销量 ${fmtQty(d.qty)}</div>`;
      tip.classList.remove("hidden");
      moveTip(e);
    };
    const moveTip = (e) => {
      tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 280) + "px";
      tip.style.top = (e.clientY + 14) + "px";
    };
    hit.addEventListener("mouseenter", show);
    hit.addEventListener("mousemove", moveTip);
    hit.addEventListener("mouseleave", () => tip.classList.add("hidden"));
    hit.addEventListener("focus", () => {
      const r = hit.getBoundingClientRect();
      show({ clientX: r.left, clientY: r.top });
    });
    hit.addEventListener("blur", () => tip.classList.add("hidden"));
  });
}

function barSvg(x, yTop, w, h, color, row, kind) {
  if (h <= 0) return "";
  const r = 4; // 4px 圆角数据端，底部直角贴合基线
  const tip = JSON.stringify({
    color, value: row[kind + "_amount"], qty: row[kind + "_qty"],
    cat: row.category_name,
    caliber: kind === "pointwise" ? "交易时口径" : "当前口径",
  }).replace(/"/g, "&quot;");
  // 命中区 ≥24px：透明 hit 矩形覆盖柱及周边。
  return `
    <path d="${roundedTopRect(x, yTop, w, h, r)}" fill="${color}"></path>
    <rect class="hit" data-tip="${tip}" tabindex="0"
          x="${x - 6}" y="${Math.max(0, yTop - 8)}"
          width="${w + 12}" height="${h + 12}" rx="2"/>`;
}
function roundedTopRect(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y}
          L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r}
          L${x + w},${y + h} Z`;
}
function niceCeil(v) {
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1))));
  const n = v / mag;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * mag;
}
function compactMoney(v) {
  if (v >= 10000) return (v / 10000).toFixed(v % 10000 === 0 ? 0 : 1) + "万";
  return String(Math.round(v));
}

$("#cmpRun").addEventListener("click", loadCompare);

/* ================================================================
 * 视图 4：报表版本 + 重算作业
 * ================================================================ */
async function loadReports() {
  const [reports, jobs] = await Promise.all([
    get("/api/reports"), get("/api/jobs"),
  ]);
  const statusText = { published: "已发布", restated: "已重述", superseded: "已替代" };
  $("#reportList").innerHTML = reports.map((r) => `
    <div class="report-item">
      <div class="head">
        <b>${r.period_key}</b>
        <span class="pill ${r.status}">v${r.version_no} · ${statusText[r.status]}</span>
        <span class="hint">${r.caliber === "pointwise" ? "交易时口径" : "当前口径"}
          · ${r.totals.total_qty} 件 · ${fmtMoney(r.totals.total_amount)}</span>
      </div>
      ${r.restate_reason ? `<div class="reason">↻ 重述原因：${esc(r.restate_reason)}</div>` : ""}
    </div>`).join("") || `<p class="hint">暂无报表</p>`;

  $("#jobsTable tbody").innerHTML = jobs.map((j) => `
    <tr>
      <td>${esc(j.change_title)}</td>
      <td class="num">${j.period_key}</td>
      <td class="num">${j.affected_products.length} 个商品</td>
      <td><span class="pill ${j.status}">${
        { done: "已重算", pending: "待处理", skipped: "无需重算" }[j.status]
      }</span></td>
    </tr>`).join("");
}

$("#reportRefresh").addEventListener("click", loadReports);
$("#reportPublish").addEventListener("click", async () => {
  const body = {
    period_key: $("#reportMonth").value,
    caliber: $("#reportCaliber").value,
  };
  const r = await post("/api/reports/publish", body);
  toast(`已发布 ${r.period_key} v${r.version_no}（${r.status === "restated" ? "重述版本" : "新版本"}）`);
  loadReports();
});

/* ================================================================
 * 视图 5：关系时间线
 * ================================================================ */
async function loadTimelineView() {
  state.products = await get("/api/products");
  const sel = $("#timelineProduct");
  sel.innerHTML = state.products
    .map((p) => `<option value="${esc(p.id)}">${esc(p.sku)} · ${esc(p.name)}</option>`)
    .join("");
  sel.value = state.products[0]?.id || "";
  await renderTimeline();
}
$("#timelineProduct").addEventListener("change", renderTimeline);

async function renderTimeline() {
  const pid = $("#timelineProduct").value;
  const [changes, history] = await Promise.all([
    get("/api/changes"),
    get(`/api/history?product_id=${pid}`),
  ]);
  const typeText = {
    move: "移动", split: "拆分", merge: "合并", create: "新建",
    retire: "停用", backfill: "补录",
  };
  $("#changeTimeline").innerHTML = changes.map((c) => `
    <div class="tl-event">
      <div><span class="tl-date">${c.effective_date}</span>
        <span class="tl-type">${typeText[c.change_type] || c.change_type}</span></div>
      <div><b>${esc(c.title)}</b></div>
      ${c.reason ? `<div class="tl-reason">${esc(c.reason)}</div>` : ""}
    </div>`).join("");

  renderIntervalBars(history.history);
  $("#timelineScope").textContent =
    `${history.product.sku} · ${history.product.name}：共 ${history.history.length} 个归属版本`;
}

/* 归属区间甘特条：按各版本区间在 2026-01 → 2026-07 时间轴上定位 */
function renderIntervalBars(intervals) {
  const axisStart = new Date("2026-01-01").getTime();
  const axisEnd = new Date("2026-07-01").getTime();
  const span = axisEnd - axisStart;
  const palette = [cssVar("--series-1"), cssVar("--series-3"),
    cssVar("--series-4"), cssVar("--series-5"), cssVar("--series-2")];
  $("#timelineBars").innerHTML = intervals.map((h, i) => {
    const from = new Date(h.effective_from).getTime();
    const to = h.effective_to ? new Date(h.effective_to).getTime() : axisEnd;
    const left = ((from - axisStart) / span) * 100;
    const width = Math.max(2, ((to - from) / span) * 100);
    const color = palette[i % palette.length];
    return `
      <div class="iv-row">
        <div class="iv-label">${esc(h.category_name)}<br>
          <span style="font-variant-numeric:tabular-nums">
            ${h.effective_from} → ${h.effective_to || "至今"}</span></div>
        <div class="iv-track">
          <div class="iv-seg" style="left:${left}%;width:${width}%;background:${color}"
               title="${esc(h.category_name)} ${h.effective_from} 起">
            ${width > 12 ? esc(h.category_code) : ""}</div>
        </div>
      </div>`;
  }).join("");
}

/* ---------------- 启动 ---------------- */
initTheme();
$("#treeDate").value = state.treeDate;
loadTree();

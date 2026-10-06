/* RAT — Repo Analysis Tool dashboard */
"use strict";

/* ---------------------------------------------------------------- helpers */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(n) {
  if (n === null || n === undefined) return "–";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e4) return (n / 1e3).toFixed(1) + "k";
  return n.toLocaleString("en-US");
}
function fmtPct(x) { return (x * 100).toFixed(x && x < 0.001 ? 3 : 1) + "%"; }
function fmtRate(x) { return x.toFixed(2); }  // churn rate is lines-per-commit, not a fraction
function fmtDate(ts) { return new Date(ts * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); }
function fmtDT(ts) { return new Date(ts * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch { /* keep */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, kind = "ok", ms = 4200) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toast-root").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function modal(title, sub, bodyNode, actions) {
  const back = document.createElement("div");
  back.className = "modal-back";
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<h2>${esc(title)}</h2>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}`;
  const body = document.createElement("div");
  body.appendChild(bodyNode);
  m.appendChild(body);
  const acts = document.createElement("div");
  acts.className = "actions";
  for (const a of actions || []) {
    const b = document.createElement("button");
    b.className = a.cls ? `btn ${a.cls}` : "btn";
    b.textContent = a.label;
    b.onclick = () => a.onclick && a.onclick(m, back);
    acts.appendChild(b);
  }
  m.appendChild(acts);
  back.appendChild(m);
  back.onclick = (e) => { if (e.target === back) back.remove(); };
  $("#modal-root").appendChild(back);
  return { back, m };
}

function downloadCSV(name, rows) {
  if (!rows.length) return toast("Nothing to export", "error");
  const cols = Object.keys(rows[0]);
  const csv = [cols.join(","), ...rows.map((r) =>
    cols.map((c) => `"${String(r[c] ?? "").replace(/"/g, '""')}"`).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------------------------------------------------------------- state */
const state = {
  repos: [],
  repoId: null,
  info: null,
  authors: [],
  treePaths: [],
  treeRoot: null,
  filters: { authors: [], path: "", mode: "all", tsFrom: null, tsTo: null, hashes: [] },
  manualSel: new Set(),
  metrics: null,
  commits: { q: "", offset: 0, limit: 50, total: 0, rows: [] },
  tab: "overview",
  sort: { key: "churn", dir: -1 },
  pollTimer: null,
  loading: 0,
};

const charts = {};

function setLoading(on) {
  state.loading += on ? 1 : -1;
  if (state.loading < 0) state.loading = 0;
  $("#loading").classList.toggle("hidden", state.loading === 0);
}

/* ---------------------------------------------------------------- repos */
async function fetchRepos() {
  state.repos = await api("/api/repos");
  renderRepoSelect();
  schedulePoll();
}

function currentRepo() { return state.repos.find((r) => r.id === state.repoId); }

function renderRepoSelect() {
  const sel = $("#repo-select");
  sel.innerHTML = state.repos.length
    ? state.repos.map((r) => {
        const icon = { ready: "●", cloning: "◐", indexing: "◐", error: "✕", pending: "○" }[r.status] || "○";
        return `<option value="${r.id}">${esc(r.name)}  ${icon}</option>`;
      }).join("")
    : `<option value="">No repositories</option>`;
  if (state.repoId) sel.value = state.repoId;
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.repos.some((r) => ["cloning", "indexing", "pending"].includes(r.status))) {
    state.pollTimer = setTimeout(async () => {
      try { await fetchRepos(); } catch { /* transient */ }
      const cur = currentRepo();
      if (cur && cur.status === "ready" && !state.info) await selectRepo(state.repoId, true);
      renderRepoStatus();
    }, 1500);
  }
}

function renderRepoStatus() {
  const box = $("#repo-status");
  const r = currentRepo();
  if (!r || r.status === "ready") { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const pct = /(\d+)%/.exec(r.detail || "");
  const msg = r.status === "error"
    ? `Error: ${r.detail || "unknown failure"}`
    : `${r.status.charAt(0).toUpperCase() + r.status.slice(1)} — ${r.detail || "working…"}`;
  box.innerHTML = `<div>${esc(msg)}</div>
    ${r.status !== "error" && pct ? `<div class="bar"><div style="width:${pct[1]}%"></div></div>` : ""}`;
}

async function selectRepo(id, silent) {
  state.repoId = id;
  state.info = null;
  state.authors = [];
  state.treePaths = [];
  state.treeRoot = null;
  state.metrics = null;
  state.manualSel = new Set();
  state.filters = { authors: [], path: "", mode: "all", tsFrom: null, tsTo: null, hashes: [] };
  resetFilterUI();
  renderRepoSelect();
  renderRepoStatus();
  const repo = currentRepo();
  if (!repo) return;
  if (repo.status === "error") { renderRepoStatus(); return; }
  if (repo.status !== "ready") { renderRepoStatus(); schedulePoll(); return; }
  try {
    if (!silent) setLoading(true);
    const [info, authors, tree] = await Promise.all([
      api(`/api/repos/${id}/info`),
      api(`/api/repos/${id}/authors`),
      api(`/api/repos/${id}/tree`),
    ]);
    state.info = info;
    state.authors = authors;
    state.treePaths = tree.paths;
    state.treeRoot = buildTree(tree.paths);
    renderTree();
    renderAuthorDD();
    await loadMetrics();
  } catch (e) {
    toast(`Failed to load repository: ${e.message}`, "error");
  } finally {
    setLoading(false);
  }
}

/* add / remove repos */
function showAddRepoModal() {
  const body = document.createElement("div");
  body.innerHTML = `
    <div class="seg">
      <button class="chip active" data-k="clone">Clone URL</button>
      <button class="chip" data-k="upload">Upload zip</button>
    </div>
    <div id="ar-clone">
      <div class="row"><input id="ar-url" class="input" placeholder="https://github.com/user/repo.git"></div>
      <div class="row"><input id="ar-name" class="input" placeholder="Display name (optional)"></div>
    </div>
    <div id="ar-upload" class="hidden">
      <div class="row"><input id="ar-file" class="input" type="file" accept=".zip"></div>
      <div class="sub">A zip archive of the repository including its .git directory.</div>
    </div>
    <div id="ar-progress" class="hidden" style="margin-top:8px"></div>`;
  let kind = "clone";
  body.querySelectorAll(".seg .chip").forEach((b) => b.onclick = () => {
    kind = b.dataset.k;
    body.querySelectorAll(".seg .chip").forEach((x) => x.classList.toggle("active", x === b));
    document.getElementById("ar-clone").classList.toggle("hidden", kind !== "clone");
    document.getElementById("ar-upload").classList.toggle("hidden", kind !== "upload");
  });

  const progressBox = body.querySelector("#ar-progress");
  const { back, m } = modal(
    "Add repository",
    "Deep-clone a remote repository or upload a zip containing .git",
    body,
    [
      { label: "Cancel", cls: "ghost", onclick: (_m, b) => b.remove() },
      {
        label: "Add", onclick: async () => {
          try {
            let repoId;
            if (kind === "clone") {
              const url = $("#ar-url").value.trim();
              if (!url) return toast("Enter a repository URL", "error");
              repoId = (await api("/api/repos/clone", { body: { url, name: $("#ar-name").value.trim() || null } })).id;
            } else {
              const f = $("#ar-file").files[0];
              if (!f) return toast("Choose a zip file", "error");
              const fd = new FormData();
              fd.append("file", f);
              setLoading(true);
              const res = await fetch("/api/repos/upload", { method: "POST", body: fd });
              setLoading(false);
              if (!res.ok) throw new Error((await res.json()).detail || "upload failed");
              repoId = (await res.json()).id;
            }
            // poll until ready
            progressBox.classList.remove("hidden");
            m.querySelector(".actions").style.display = "none";
            const poll = async () => {
              const repos = await api("/api/repos");
              const r = repos.find((x) => x.id === repoId);
              progressBox.innerHTML = `<div class="repo-status" style="display:block">${esc(r.status)} — ${esc(r.detail || "")}</div>`;
              if (r.status === "ready") {
                back.remove();
                state.repos = repos;
                await selectRepo(repoId);
                toast(`Repository "${r.name}" is ready`);
              } else if (r.status === "error") {
                progressBox.innerHTML = `<div class="toast error" style="display:block">${esc(r.detail)}</div>
                  <div class="actions" style="display:flex;justify-content:flex-end"><button class="btn ghost" onclick="this.closest('.modal-back').remove()">Close</button></div>`;
              } else {
                setTimeout(poll, 1200);
              }
            };
            poll();
          } catch (e) { toast(e.message, "error"); }
        },
      },
    ]);
}

function confirmDeleteRepo() {
  const r = currentRepo();
  if (!r) return;
  const body = document.createElement("div");
  body.innerHTML = `<p>Remove <b>${esc(r.name)}</b> and its indexed metrics from RAT?
  The original remote is not affected.</p>`;
  modal("Remove repository", "", body, [
    { label: "Cancel", cls: "ghost", onclick: (_m, b) => b.remove() },
    {
      label: "Remove", cls: "danger", onclick: async (_m, b) => {
        try {
          await api(`/api/repos/${r.id}`, { method: "DELETE" });
          b.remove();
          state.repoId = null;
          await fetchRepos();
          const first = state.repos.find((x) => x.status === "ready") || state.repos[0];
          if (first) await selectRepo(first.id); else renderAll();
          toast(`Removed "${r.name}"`);
        } catch (e) { toast(e.message, "error"); }
      },
    },
  ]);
}

/* ---------------------------------------------------------------- tree */
function buildTree(paths) {
  const root = { name: "", path: "", dir: true, children: new Map() };
  for (const p of paths) {
    const parts = p.split("/");
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const isFile = i === parts.length - 1;
      if (!node.children.has(name)) {
        node.children.set(name, {
          name, path: parts.slice(0, i + 1).join("/"), dir: !isFile,
          children: isFile ? null : new Map(),
        });
      }
      node = node.children.get(name);
    }
  }
  return root;
}

const expandedDirs = new Set();

function renderTree() {
  const host = $("#tree");
  const q = $("#tree-search").value.trim().toLowerCase();
  host.innerHTML = "";
  if (!state.treeRoot) {
    host.innerHTML = `<div class="tree-empty">No repository selected</div>`;
    return;
  }
  if (q) {
    const hits = state.treePaths.filter((p) => p.toLowerCase().includes(q)).slice(0, 400);
    host.innerHTML = hits.length
      ? hits.map((p) => {
          const isDir = state.treePaths.some((o) => o.startsWith(p + "/"));
          return `<div class="tree-row ${state.filters.path === p ? "active" : ""}" data-path="${esc(p)}" data-dir="${isDir}">
            <span class="icon">${isDir ? "▸" : "•"}</span><span class="name" title="${esc(p)}">${esc(p)}</span></div>`;
        }).join("")
      : `<div class="tree-empty">No paths match</div>`;
    return;
  }
  const renderLevel = (node, depth, hostEl) => {
    const kids = [...node.children.values()].sort((a, b) =>
      (b.dir - a.dir) || a.name.localeCompare(b.name));
    for (const kid of kids) {
      const open = expandedDirs.has(kid.path);
      const row = document.createElement("div");
      row.className = "tree-row" + (state.filters.path === kid.path ? " active" : "");
      row.style.paddingLeft = (6 + depth * 14) + "px";
      row.dataset.path = kid.path;
      row.dataset.dir = kid.dir;
      row.innerHTML = `<span class="caret ${open ? "open" : ""}">${kid.dir ? "▸" : ""}</span>
        <span class="icon">${kid.dir ? "" : "•"}</span><span class="name" title="${esc(kid.path)}">${esc(kid.name)}</span>`;
      row.querySelector(".caret").style.visibility = kid.dir ? "visible" : "hidden";
      hostEl.appendChild(row);
      if (kid.dir && open) {
        const sub = document.createElement("div");
        hostEl.appendChild(sub);
        renderLevel(kid, depth + 1, sub);
      }
    }
  };
  renderLevel(state.treeRoot, 0, host);
}

/* ---------------------------------------------------------------- authors dd */
function renderAuthorDD() {
  const btn = $("#author-dd-btn");
  const n = state.filters.authors.length;
  btn.textContent = n === 0 ? "All authors" : n === 1
    ? (state.authors.find((a) => a.id === state.filters.authors[0]) || {}).name || "1 author"
    : `${n} authors`;
  const q = $("#author-dd-search").value.trim().toLowerCase();
  const list = $("#author-dd-list");
  list.innerHTML = state.authors
    .filter((a) => !q || a.name.toLowerCase().includes(q) || a.email.toLowerCase().includes(q))
    .slice(0, 400)
    .map((a) => {
      const checked = state.filters.authors.includes(a.id);
      return `<label class="dd-item"><input type="checkbox" data-id="${a.id}" ${checked ? "checked" : ""}>
        <span class="who"><span class="nm">${esc(a.name)}${a.aliases.length ? ` <span title="${a.aliases.length} merged identit(y|ies)">(+${a.aliases.length})</span>` : ""}</span>
        <span class="em">${esc(a.email)}</span></span><span class="ct">${fmt(a.commits)}</span></label>`;
    }).join("") || `<div class="tree-empty">No match</div>`;
}

/* ---------------------------------------------------------------- metrics */
function currentFilterBody() {
  const f = state.filters;
  return {
    authors: f.authors.length ? f.authors : null,
    path: f.path || "",
    mode: f.mode,
    ts_from: f.mode === "range" ? f.tsFrom : null,
    ts_to: f.mode === "range" ? f.tsTo : null,
    hashes: f.mode === "manual" ? [...state.manualSel] : null,
  };
}

async function loadMetrics() {
  if (!state.repoId || !currentRepo() || currentRepo().status !== "ready") { renderAll(); return; }
  setLoading(true);
  try {
    state.metrics = await api(`/api/repos/${state.repoId}/metrics`, { body: currentFilterBody() });
    renderAll();
  } catch (e) {
    toast(`Metrics failed: ${e.message}`, "error");
  } finally { setLoading(false); }
}

/* ---------------------------------------------------------------- renderers */
function renderAll() {
  renderScopeUI();
  renderSetInfo();
  renderOverview();
  renderExplorer();
  renderAuthorsTab();
  renderCommitsTab();
}

function renderSetInfo() {
  const m = state.metrics;
  $("#set-info").textContent = m ? `${fmt(m.commit_count)} commits in set` : "—";
  $("#btn-manual-info").classList.toggle("hidden", state.filters.mode !== "manual");
  $("#btn-manual-info").textContent = `${state.manualSel.size} commits selected`;
}

function renderScopeUI() {
  const p = state.filters.path;
  $("#btn-copy-path").classList.toggle("hidden", !p);
  let html = `<a data-p="">root</a>`;
  if (p) {
    const parts = p.split("/");
    let acc = "";
    for (let i = 0; i < parts.length; i++) {
      acc = acc ? acc + "/" + parts[i] : parts[i];
      html += ` / ${i === parts.length - 1 ? `<span class="cur">${esc(parts[i])}</span>` : `<a data-p="${esc(acc)}">${esc(parts[i])}</a>`}`;
    }
  }
  $("#scope-crumb").innerHTML = html;
  $$("#scope-crumb a").forEach((a) => a.onclick = () => setScope(a.dataset.p));
}

function setScope(path) {
  state.filters.path = path || "";
  renderTree();
  loadMetrics();
}

function kpiCard(label, value, cls) {
  return `<div class="kpi"><div class="v ${cls || ""}">${value}</div><div class="l">${label}</div></div>`;
}

function renderOverview() {
  const host = $("#tab-overview");
  const m = state.metrics;
  if (!m) {
    host.innerHTML = state.repos.length
      ? `<div class="empty"><div class="big">◔</div><b>Repository is not ready yet</b>
         <div class="hint">Cloning or indexing is in progress — this page refreshes automatically.</div></div>`
      : `<div class="empty"><div class="big">⌂</div><b>No repositories yet</b>
         <div class="hint">Add a repository by cloning a remote URL or uploading a zip archive that contains the .git directory.</div>
         <button class="btn" onclick="showAddRepoModal()">Add repository</button></div>`;
    return;
  }
  const t = m.totals;
  const authorsActive = m.authors.filter((a) => a.commits > 0 || a.churn > 0).length;
  host.innerHTML = `
    <div class="kpis">
      ${kpiCard("Commits in set", fmt(m.commit_count))}
      ${kpiCard("Authors", fmt(authorsActive))}
      ${kpiCard("Files touched", fmt(m.files_touched))}
      ${kpiCard("Lines added", fmt(t.added), "pos")}
      ${kpiCard("Lines removed", fmt(t.removed), "neg")}
      ${kpiCard("Net growth", (t.growth >= 0 ? "+" : "") + fmt(t.growth), t.growth >= 0 ? "pos" : "neg")}
      ${kpiCard("Churn", fmt(t.churn))}
      ${kpiCard("Modifications", fmt(t.mods))}
      ${kpiCard("Churn rate (lines/commit)", fmtRate(t.churn_rate))}
    </div>
    <div class="charts">
      <div class="chart-card" style="grid-column:1/-1"><h3>Churn over time</h3><div id="ch-timeline" class="chart"></div></div>
      <div class="chart-card"><h3>Scope breakdown (churn)</h3><div id="ch-treemap" class="chart tall"></div></div>
      <div class="chart-card"><h3>Top files by churn</h3><div id="ch-topfiles" class="chart tall"></div></div>
      <div class="chart-card" style="grid-column:1/-1"><h3>Author churn in scope</h3><div id="ch-authors" class="chart"></div></div>
    </div>`;
  drawTimeline(m.series);
  drawTreemap(m.children);
  drawTopFiles(m.top_files);
  drawAuthorChurn(m.authors.slice(0, 20));
}

function drawTimeline(series) {
  const c = ensureChart("ch-timeline");
  if (!c) return;
  if (!series || !series.length) return c.setOption({ xAxis: { data: [] }, series: [] });
  c.setOption({
    animation: false, grid: { left: 60, right: 55, top: 26, bottom: 44 },
    tooltip: { trigger: "axis", backgroundColor: "#1a2431", borderColor: "#263242", textStyle: { color: "#d8e0ea", fontSize: 12 } },
    legend: { textStyle: { color: "#8b98a9", fontSize: 12 }, top: 0 },
    xAxis: { type: "category", data: series.map((s) => s.month), axisLine: { lineStyle: { color: "#263242" } }, axisLabel: { color: "#8b98a9" } },
    yAxis: [
      { type: "log", logBase: 10, min: 1, axisLabel: { color: "#8b98a9", formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: "#1f2a38" } } },
      { type: "log", logBase: 10, min: 1, axisLabel: { color: "#8b98a9", formatter: (v) => fmt(v) }, splitLine: { show: false } },
    ],
    series: [
      { name: "Added", type: "line", data: series.map((s) => s.added || null), symbol: "none",
        lineStyle: { color: "#34d399", width: 1.4 }, areaStyle: { color: "rgba(52,211,153,.14)" } },
      { name: "Removed", type: "line", data: series.map((s) => s.removed || null), symbol: "none",
        lineStyle: { color: "#f87171", width: 1.4 }, areaStyle: { color: "rgba(248,113,113,.12)" } },
      { name: "Churn", type: "line", data: series.map((s) => s.churn || null), symbol: "none",
        lineStyle: { color: "#fbbf24", width: 1.6 } },
      { name: "Commits", type: "line", yAxisIndex: 1, data: series.map((s) => s.commits || null), symbol: "none",
        lineStyle: { color: "#60a5fa", width: 1.4, type: "dashed" } },
    ],
  });
}

function drawTreemap(children) {
  const c = ensureChart("ch-treemap");
  if (!c) return;
  const data = (children || [])
    .filter((x) => x.churn > 0)
    .slice(0, 60)
    .map((x) => ({ name: x.name, value: x.churn, path: x.path, itemStyle: { color: x.type === "dir" ? "#2e5c9e" : "#1f7a5a" } }));
  c.setOption({
    animation: false,
    tooltip: { backgroundColor: "#1a2431", borderColor: "#263242", textStyle: { color: "#d8e0ea", fontSize: 12 },
      formatter: (p) => `<b>${esc(p.name)}</b><br/>churn: ${fmt(p.value)}` },
    series: [{
      type: "treemap", data: data.length ? data : [{ name: "no churn in scope", value: 1, itemStyle: { color: "#1a2431" } }],
      roam: false, nodeClick: false, breadcrumb: { show: false },
      label: { color: "#d8e0ea", fontSize: 11 }, upperLabel: { show: true, height: 18, color: "#9db4d0" },
      itemStyle: { borderColor: "#0e1319", borderWidth: 1.5, gapWidth: 2 },
      levels: [{ itemStyle: { gapWidth: 3 } }],
    }],
  });
  c.off("click");
  c.on("click", (p) => { if (p.data && p.data.path) setScope(p.data.path); });
}

function drawTopFiles(files) {
  const c = ensureChart("ch-topfiles");
  if (!c) return;
  const list = (files || []).filter((f) => f.churn > 0).slice(0, 15).reverse();
  c.setOption({
    animation: false, grid: { left: 10, right: 60, top: 6, bottom: 10, containLabel: true },
    tooltip: { backgroundColor: "#1a2431", borderColor: "#263242", textStyle: { color: "#d8e0ea", fontSize: 12 },
      formatter: (p) => { const f = list[p[0].dataIndex]; return `<b>${esc(f.path)}</b><br/>churn: ${fmt(f.churn)}<br/>growth: ${f.growth >= 0 ? "+" : ""}${fmt(f.growth)}<br/>mods: ${fmt(f.mods)}`; } },
    xAxis: { type: "value", axisLabel: { color: "#8b98a9", formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: "#1f2a38" } } },
    yAxis: { type: "category", data: list.map((f) => f.name), axisLabel: { color: "#d8e0ea", fontSize: 12 } },
    series: [{ type: "bar", data: list.map((f) => f.churn), itemStyle: { color: "#4f9cf9", borderRadius: [0, 4, 4, 0] }, barMaxWidth: 18 }],
  });
  c.off("click");
  c.on("click", (p) => { const f = list[p.dataIndex]; if (f) setScope(f.path); });
}

const AUTHOR_COLORS = ["#4f9cf9", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#38bdf8", "#fb923c", "#4ade80", "#e879f9", "#f472b6"];
function drawAuthorChurn(authors) {
  const c = ensureChart("ch-authors");
  if (!c) return;
  const list = authors.filter((a) => a.churn > 0).reverse();
  c.setOption({
    animation: false, grid: { left: 10, right: 60, top: 6, bottom: 10, containLabel: true },
    tooltip: { backgroundColor: "#1a2431", borderColor: "#263242", textStyle: { color: "#d8e0ea", fontSize: 12 },
      formatter: (p) => { const a = list[p[0].dataIndex]; return `<b>${esc(a.name)}</b><br/>churn: ${fmt(a.churn)} (${fmtPct(a.ownership)})<br/>commits: ${fmt(a.commits)}`; } },
    xAxis: { type: "value", axisLabel: { color: "#8b98a9", formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: "#1f2a38" } } },
    yAxis: { type: "category", data: list.map((a) => a.name), axisLabel: { color: "#d8e0ea", fontSize: 12.5 } },
    series: [{ type: "bar", data: list.map((a, i) => ({ value: a.churn, itemStyle: { color: AUTHOR_COLORS[i % 10] } })),
      barMaxWidth: 18, borderRadius: [0, 4, 4, 0] }],
  });
}

function ensureChart(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  if (!charts[id] || charts[id].isDisposed?.()) {
    charts[id] = echarts.init(el);
  }
  return charts[id];
}

/* ---------------------------------------------------------------- explorer */
const COLS = [
  { key: "name", label: "Name", sortable: true },
  { key: "type", label: "Type" },
  { key: "added", label: "Added", num: true, sortable: true },
  { key: "removed", label: "Removed", num: true, sortable: true },
  { key: "growth", label: "Growth", num: true, sortable: true },
  { key: "churn", label: "Churn", num: true, sortable: true },
  { key: "mods", label: "Mods", num: true, sortable: true },
  { key: "mod_freq", label: "Mod freq", num: true, sortable: true },
  { key: "churn_rate", label: "Churn rate", num: true, sortable: true },
];

function renderExplorer() {
  const host = $("#tab-explorer");
  const m = state.metrics;
  if (!m) { host.innerHTML = emptyRepo(); return; }

  if (m.scope.type === "file") { renderFileView(host, m); return; }

  const rows = [...(m.children || [])];
  const { key, dir } = state.sort;
  rows.sort((a, b) => {
    let va = a[key], vb = b[key];
    if (typeof va === "string") return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });
  const maxChurn = Math.max(1, ...rows.map((r) => r.churn));
  host.innerHTML = `
    <div class="section-head">
      <h2>Explorer</h2><span class="sub">${esc(state.filters.path || "repository root")} — immediate children with metrics in the current commit set</span>
      <span class="spacer"></span>
      <button class="btn ghost tiny" id="btn-csv-children">Export CSV</button>
    </div>
    <div class="table-wrap">
    <table><thead><tr>${COLS.map((c) => `<th class="${c.sortable ? "sortable" : ""} ${c.num ? "num" : ""}" data-k="${c.key}">
      ${c.label}${c.sortable && key === c.key ? (dir === -1 ? " ▾" : " ▴") : ""}</th>`).join("")}</tr></thead>
    <tbody>${rows.length ? rows.map((r) => explorerRow(r, maxChurn)).join("")
      : `<tr><td colspan="9"><div class="empty" style="padding:30px">No file activity in this scope for the current commit set.</div></td></tr>`}</tbody></table></div>`;
  $$("#tab-explorer th.sortable").forEach((th) => th.onclick = () => {
    const k = th.dataset.k;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "name" ? 1 : -1) };
    renderExplorer();
  });
  $$("#tab-explorer .rowlink").forEach((el) => el.onclick = () => setScope(el.dataset.path));
  $("#btn-csv-children").onclick = () =>
    downloadCSV(`rat-${state.filters.path.replace(/\//g, "_") || "root"}-children.csv`, rows);
}

function explorerRow(r, maxChurn) {
  const gcls = r.growth > 0 ? "growth-pos" : r.growth < 0 ? "growth-neg" : "";
  return `<tr>
    <td class="name-cell"><span class="rowlink" data-path="${esc(r.path)}">${r.dir ? "▸ " : ""}${esc(r.name)}</span></td>
    <td class="type-${r.type}">${r.type}</td>
    <td class="num add">${fmt(r.added)}</td>
    <td class="num rem">${fmt(r.removed)}</td>
    <td class="num ${gcls}">${(r.growth >= 0 ? "+" : "") + fmt(r.growth)}</td>
    <td class="num bar-cell"><div class="fill" style="width:${Math.max(3, (r.churn / maxChurn) * 100)}%"></div><span>${fmt(r.churn)}</span></td>
    <td class="num">${fmt(r.mods)}</td>
    <td class="num">${fmtPct(r.mod_freq)}</td>
    <td class="num">${fmtRate(r.churn_rate)}</td></tr>`;
}

function renderFileView(host, m) {
  const d = m.file_detail || { authors: [] };
  const t = m.totals;
  const maxChurn = Math.max(1, ...d.authors.map((a) => a.churn));
  host.innerHTML = `
    <div class="section-head">
      <h2>File: ${esc(m.scope.path)}</h2>
      <span class="sub">${fmt(m.commit_count)} commits in set</span>
      <span class="spacer"></span>
      <button class="btn ghost tiny" id="btn-csv-file">Export CSV</button>
    </div>
    <div class="kpis">
      ${kpiCard("Lines added", fmt(t.added), "pos")}
      ${kpiCard("Lines removed", fmt(t.removed), "neg")}
      ${kpiCard("Net growth", (t.growth >= 0 ? "+" : "") + fmt(t.growth), t.growth >= 0 ? "pos" : "neg")}
      ${kpiCard("Churn", fmt(t.churn))}
      ${kpiCard("Modifications", fmt(t.mods))}
      ${kpiCard("Mod frequency", fmtPct(t.mod_freq))}
    </div>
    <div class="section-head" style="margin-top:18px"><h2>Author ownership</h2>
      <span class="sub">share of churn on this file within the current commit set</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Author</th><th class="num">Added</th><th class="num">Removed</th><th class="num">Churn</th>
      <th class="num">Mods</th><th>Ownership</th></tr></thead>
      <tbody>${d.authors.length ? d.authors.map((a) => `<tr>
        <td title="${esc(a.email)}">${esc(a.name)}</td>
        <td class="num add">${fmt(a.added)}</td><td class="num rem">${fmt(a.removed)}</td>
        <td class="num bar-cell"><div class="fill" style="width:${Math.max(3, (a.churn / maxChurn) * 100)}%"></div><span>${fmt(a.churn)}</span></td>
        <td class="num">${fmt(a.mods)}</td>
        <td><span class="own-bar" style="width:${Math.max(2, a.ownership * 140)}px;background:${a.ownership > .5 ? "#34d399" : a.ownership > .2 ? "#fbbf24" : "#60a5fa"}"></span>${fmtPct(a.ownership)}</td></tr>`).join("")
      : `<tr><td colspan="6"><div class="empty" style="padding:30px">No author activity on this file in the current commit set.</div></td></tr>`}</tbody>
    </table></div>`;
  $("#btn-csv-file").onclick = () => downloadCSV(`rat-${m.scope.path.replace(/\//g, "_")}-authors.csv`,
    d.authors.map((a) => ({ author: a.name, email: a.email, added: a.added, removed: a.removed, churn: a.churn, mods: a.mods, ownership: a.ownership })));
}

function emptyRepo() {
  return `<div class="empty"><div class="big">⌂</div><b>No repository selected</b></div>`;
}

/* ---------------------------------------------------------------- authors tab */
const authorSel = new Set();

function renderAuthorsTab() {
  const host = $("#tab-authors");
  const m = state.metrics;
  if (!m) { host.innerHTML = emptyRepo(); return; }
  const rows = m.authors || [];
  const maxChurn = Math.max(1, ...rows.map((a) => a.churn));
  const scope = state.filters.path ? `scope: ${state.filters.path}` : "scope: repository root";
  host.innerHTML = `
    <div class="section-head">
      <h2>Authors</h2>
      <span class="sub">${esc(scope)} · churn, modifications and ownership are restricted to the scope; commits span the whole commit set</span>
      <span class="spacer"></span>
      <button class="btn ghost tiny" id="btn-csv-authors">Export CSV</button>
      <button class="btn tiny ${authorSel.size ? "" : "hidden"}" id="btn-merge">Merge selected (${authorSel.size})</button>
      <button class="btn ghost tiny ${authorSel.size ? "" : "hidden"}" id="btn-merge-clear">Clear</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" id="author-checkall" title="Select all"></th>
      <th>Author</th><th class="num">Commits</th><th class="num">Added</th><th class="num">Removed</th>
      <th class="num">Churn</th><th class="num">Mods</th><th>Ownership</th><th>Merged identities</th></tr></thead>
      <tbody>${rows.length ? rows.map((a) => {
        const group = state.authors.find((g) => g.id === a.id);
        const aliases = (group && group.aliases) || [];
        return `<tr>
        <td><input type="checkbox" data-id="${a.id}" ${authorSel.has(a.id) ? "checked" : ""}></td>
        <td title="${esc(a.email)}"><b>${esc(a.name)}</b><div class="em" style="color:var(--muted);font-size:11.5px">${esc(a.email)}</div></td>
        <td class="num">${fmt(a.commits)}</td>
        <td class="num add">${fmt(a.added)}</td>
        <td class="num rem">${fmt(a.removed)}</td>
        <td class="num bar-cell"><div class="fill" style="width:${Math.max(3, (a.churn / maxChurn) * 100)}%"></div><span>${fmt(a.churn)}</span></td>
        <td class="num">${fmt(a.mods)}</td>
        <td><span class="own-bar" style="width:${Math.max(2, a.ownership * 120)}px;background:${a.ownership > .5 ? "#34d399" : a.ownership > .2 ? "#fbbf24" : "#60a5fa"}"></span>${fmtPct(a.ownership)}</td>
        <td>${aliases.map((al) => `<span class="alias" title="unmerge">${esc(al.name)} &lt;${esc(al.email)}&gt;<button data-unmerge="${al.id}" title="Split this identity back out">×</button></span>`).join("")}</td>
      </tr>`; }).join("")
      : `<tr><td colspan="9"><div class="empty" style="padding:30px">No authors match the current filters.</div></td></tr>`}</tbody>
    </table></div>
    <p class="sub" style="color:var(--muted);font-size:12.5px;margin-top:10px">
      Merged identities update all metrics instantly. .mailmap entries are applied automatically at ingestion;
      use the checkboxes to merge identities manually.</p>`;
  $$("#tab-authors tbody input[type=checkbox][data-id]").forEach((cb) => cb.onchange = () => {
    cb.checked ? authorSel.add(+cb.dataset.id) : authorSel.delete(+cb.dataset.id);
    renderAuthorsTab();
  });
  const checkall = $("#author-checkall");
  if (checkall) checkall.onchange = () => {
    if (checkall.checked) rows.forEach((a) => authorSel.add(a.id));
    else authorSel.clear();
    renderAuthorsTab();
  };
  $("#btn-csv-authors").onclick = () => downloadCSV("rat-authors.csv", rows);
  $("#btn-merge-clear").onclick = () => { authorSel.clear(); renderAuthorsTab(); };
  $("#btn-merge").onclick = showMergeModal;
  $$("#tab-authors [data-unmerge]").forEach((b) => b.onclick = async () => {
    try {
      await api(`/api/repos/${state.repoId}/authors/unmerge`, { body: { author_id: +b.dataset.unmerge } });
      await refreshAuthors();
      toast("Identity split back out");
    } catch (e) { toast(e.message, "error"); }
  });
}

async function refreshAuthors() {
  state.authors = await api(`/api/repos/${state.repoId}/authors`);
  authorSel.clear();
  await loadMetrics();
}

function showMergeModal() {
  const ids = [...authorSel];
  const groups = ids.map((id) => state.authors.find((a) => a.id === id)).filter(Boolean);
  const body = document.createElement("div");
  body.innerHTML = `
    <p>Merge <b>${groups.length}</b> selected identities into one author. All commits and metrics of the
    source identities will be attributed to the target.</p>
    ${groups.map((g, i) => `
      <label class="radio-row"><input type="radio" name="mtarget" value="${g.id}" ${i === 0 ? "checked" : ""}>
        <span class="who"><b>${esc(g.name)}</b><div style="color:var(--muted);font-size:12px">${esc(g.email)}</div></span></label>`).join("")}
    <label class="radio-row"><input type="radio" name="mtarget" value="new">
      <span class="who"><b>New identity</b></span></label>
    <div class="row" id="new-id" style="display:none">
      <input id="merge-name" class="input" placeholder="Display name">
      <input id="merge-email" class="input" placeholder="email@example.com">
    </div>`;
  body.querySelectorAll("input[name=mtarget]").forEach((r) => r.onchange = () => {
    body.querySelector("#new-id").style.display = r.value === "new" && r.checked ? "flex" : "none";
  });
  modal("Merge authors", "", body, [
    { label: "Cancel", cls: "ghost", onclick: (_m, b) => b.remove() },
    {
      label: "Merge", onclick: async (_m, b) => {
        const val = body.querySelector("input[name=mtarget]:checked").value;
        try {
          if (val === "new") {
            const name = $("#merge-name").value.trim(), email = $("#merge-email").value.trim();
            if (!name || !email) return toast("Provide a name and email", "error");
            await api(`/api/repos/${state.repoId}/authors/merge`,
              { body: { source_ids: ids, target_name: name, target_email: email } });
          } else {
            await api(`/api/repos/${state.repoId}/authors/merge`,
              { body: { source_ids: ids.filter((i) => i !== +val), target_id: +val } });
          }
          b.remove();
          await refreshAuthors();
          toast("Authors merged");
        } catch (e) { toast(e.message, "error"); }
      },
    },
  ]);
}

/* ---------------------------------------------------------------- commits tab */
async function loadCommits() {
  if (!state.repoId) return;
  const { q, offset, limit } = state.commits;
  const data = await api(`/api/repos/${state.repoId}/commits?query=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`);
  state.commits.total = data.total;
  state.commits.rows = data.rows;
  renderCommitsTab();
}

function renderCommitsTab() {
  const host = $("#tab-commits");
  if (!state.metrics) { host.innerHTML = emptyRepo(); return; }
  const { q, offset, limit, total, rows } = state.commits;
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.floor(offset / limit) + 1;
  const active = state.filters.mode === "manual";
  host.innerHTML = `
    <div class="section-head">
      <h2>Commits</h2>
      <span class="sub">${fmt(total)} non-merge commits reachable from HEAD · search by hash, subject or author</span>
      <span class="spacer"></span>
      <input id="commit-q" class="input" type="search" placeholder="Search commits…" value="${esc(q)}" style="width:260px">
    </div>
    ${active ? `<div style="margin-bottom:10px"><button class="btn tiny" id="btn-use-sel">Use ${state.manualSel.size} selected commits as filter</button>
      <button class="btn ghost tiny" id="btn-clear-sel">Clear manual filter</button></div>` : ""}
    <div class="table-wrap">
    <table><thead><tr><th><input type="checkbox" id="commit-checkall" title="Select page"></th>
      <th>Hash</th><th>Date</th><th>Author</th><th>Subject</th></tr></thead>
    <tbody>${rows.map((c) => `<tr>
      <td><input type="checkbox" data-h="${c.hash}" ${state.manualSel.has(c.hash) ? "checked" : ""}></td>
      <td class="hash" title="${c.hash}">${c.hash.slice(0, 10)}</td>
      <td>${fmtDT(c.ts)}</td>
      <td>${esc(c.author)}</td>
      <td class="subject" title="${esc(c.subject)}">${esc(c.subject)}</td></tr>`).join("")
      || `<tr><td colspan="5"><div class="empty" style="padding:30px">No commits match.</div></td></tr>`}</tbody></table>
    <div class="pager">
      <button class="btn ghost tiny" id="pg-prev" ${page <= 1 ? "disabled" : ""}>‹ Prev</button>
      <span>page ${page} / ${pages}</span>
      <button class="btn ghost tiny" id="pg-next" ${page >= pages ? "disabled" : ""}>Next ›</button>
      <span class="spacer" style="flex:1"></span>
      <span>${state.manualSel.size} selected for manual filter</span>
    </div></div>`;
  $("#commit-q").oninput = debounce(() => { state.commits.q = $("#commit-q").value; state.commits.offset = 0; loadCommits(); }, 300);
  $("#pg-prev").onclick = () => { state.commits.offset = Math.max(0, offset - limit); loadCommits(); };
  $("#pg-next").onclick = () => { state.commits.offset = Math.min((pages - 1) * limit, offset + limit); loadCommits(); };
  $$("#tab-commits input[data-h]").forEach((cb) => cb.onchange = () => {
    cb.checked ? state.manualSel.add(cb.dataset.h) : state.manualSel.delete(cb.dataset.h);
    $("#btn-manual-info").textContent = `${state.manualSel.size} commits selected`;
    const cnt = host.querySelector(".pager span:last-child");
    if (cnt) cnt.textContent = `${state.manualSel.size} selected for manual filter`;
  });
  const checkall = $("#commit-checkall");
  if (checkall) checkall.onchange = () => {
    rows.forEach((c) => checkall.checked ? state.manualSel.add(c.hash) : state.manualSel.delete(c.hash));
    renderCommitsTab(); renderSetInfo();
  };
  const use = $("#btn-use-sel");
  if (use) use.onclick = async () => {
    if (!state.manualSel.size) return toast("Select at least one commit", "error");
    state.filters.mode = "manual";
    state.filters.hashes = [...state.manualSel];
    setModeChips("manual");
    await loadMetrics();
    toast(`Filtered to ${state.manualSel.size} commits`);
  };
  const clear = $("#btn-clear-sel");
  if (clear) clear.onclick = async () => {
    state.filters.mode = "all";
    setModeChips("all");
    await loadMetrics();
  };
}

/* ---------------------------------------------------------------- filter UI */
function setModeChips(mode) {
  $$("#commit-modes .chip").forEach((c) => c.classList.toggle("active", c.dataset.mode === mode));
  $("#range-inputs").classList.toggle("hidden", mode !== "range");
}

function resetFilterUI() {
  setModeChips("all");
  $("#date-from").value = "";
  $("#date-to").value = "";
  $("#author-dd-search").value = "";
  authorSel.clear();
  expandedDirs.clear();
}

function dateToTs(v, exclusive) {
  if (!v) return null;
  const d = new Date(v + "T00:00:00");
  if (isNaN(d)) return null;
  return Math.floor(d.getTime() / 1000) + (exclusive ? 86400 : 0);
}

function initFilterEvents() {
  $$("#commit-modes .chip").forEach((c) => c.onclick = async () => {
    const mode = c.dataset.mode;
    if (mode === "manual") {
      if (!state.manualSel.size) {
        state.tab = "commits";
        switchTab("commits");
        toast("Select commits with the checkboxes, then press “Use selected commits”");
        return;
      }
    }
    state.filters.mode = mode;
    setModeChips(mode);
    await loadMetrics();
  });
  $("#date-from").onchange = async () => {
    state.filters.tsFrom = dateToTs($("#date-from").value, false);
    state.filters.mode = "range";
    setModeChips("range");
    await loadMetrics();
  };
  $("#date-to").onchange = async () => {
    state.filters.tsTo = dateToTs($("#date-to").value, true);
    state.filters.mode = "range";
    setModeChips("range");
    await loadMetrics();
  };
  $("#btn-manual-info").onclick = () => switchTab("commits");
  $("#btn-scope-root").onclick = () => setScope("");
  $("#btn-copy-path").onclick = () => {
    navigator.clipboard?.writeText(state.filters.path).then(() => toast("Path copied"));
  };
  $("#btn-reset-filters").onclick = async () => {
    state.filters = { authors: [], path: "", mode: "all", tsFrom: null, tsTo: null, hashes: [] };
    state.manualSel.clear();
    resetFilterUI();
    renderAuthorDD();
    renderTree();
    await loadMetrics();
    toast("Filters reset");
  };

  // author dropdown
  $("#author-dd-btn").onclick = (e) => {
    e.stopPropagation();
    $("#author-dd-panel").classList.toggle("hidden");
    renderAuthorDD();
  };
  document.addEventListener("click", (e) => {
    const panel = $("#author-dd-panel");
    if (!panel.classList.contains("hidden") && !panel.parentElement.contains(e.target) && e.target.id !== "author-dd-btn")
      panel.classList.add("hidden");
  });
  $("#author-dd-search").oninput = renderAuthorDD;
  $("#author-dd-done").onclick = () => $("#author-dd-panel").classList.add("hidden");
  $("#author-dd-clear").onclick = async () => {
    state.filters.authors = [];
    renderAuthorDD();
    await loadMetrics();
  };
  $("#author-dd-list").addEventListener("change", async (e) => {
    const id = +e.target.dataset.id;
    if (!id) return;
    const set = new Set(state.filters.authors);
    e.target.checked ? set.add(id) : set.delete(id);
    state.filters.authors = [...set];
    await loadMetrics();
  });
}

/* ---------------------------------------------------------------- tabs */
function switchTab(name) {
  state.tab = name;
  $$("#tabs button[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  if (name === "commits" && !state.commits.rows.length) loadCommits();
  Object.values(charts).forEach((c) => c.resize());
}

/* ---------------------------------------------------------------- tree events */
function initTreeEvents() {
  $("#tree").addEventListener("click", (e) => {
    const row = e.target.closest(".tree-row");
    if (!row) return;
    const path = row.dataset.path;
    if (row.dataset.dir === "true" && !$("#tree-search").value.trim()) {
      expandedDirs.has(path) ? expandedDirs.delete(path) : expandedDirs.add(path);
      renderTree();
      setScope(path);
    } else {
      setScope(path);
    }
  });
  $("#tree-search").oninput = debounce(renderTree, 200);
}

/* ---------------------------------------------------------------- init */
function init() {
  $("#btn-add-repo").onclick = showAddRepoModal;
  $("#btn-del-repo").onclick = confirmDeleteRepo;
  $("#repo-select").onchange = (e) => selectRepo(+e.target.value);
  $$("#tabs button[data-tab]").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  window.addEventListener("resize", debounce(() => Object.values(charts).forEach((c) => c.resize()), 150));
  initFilterEvents();
  initTreeEvents();
  fetchRepos().then(async () => {
    const first = state.repos.find((r) => r.status === "ready") || state.repos[0];
    if (first) await selectRepo(first.id);
    else renderAll();
  }).catch((e) => toast(e.message, "error"));
}

init();

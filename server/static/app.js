(function () {
  "use strict";

  /* ── State ── */
  let config = {};
  let hosts = [];
  let historyData = {};
  let refreshTimer = null;
  const sparkPlots = new Map();

  /* ── DOM refs ── */
  const $grid = document.getElementById("host-grid");
  const $summary = document.getElementById("summary-bar");
  const $serverTime = document.getElementById("server-time");
  const $filterStatus = document.getElementById("filter-status");
  const $sortBy = document.getElementById("sort-by");
  const $themeToggle = document.getElementById("theme-toggle");
  const $refreshBtn = document.getElementById("refresh-btn");
  const $overlay = document.getElementById("modal-overlay");
  const $modalClose = document.getElementById("modal-close");
  const $toast = document.getElementById("toast");

  /* ── Init ── */
  async function init() {
    loadTheme();
    showSkeletons();
    await Promise.all([fetchConfig(), fetchHosts(), fetchHistory()]);
    render();
    startAutoRefresh();

    $filterStatus.addEventListener("change", render);
    $sortBy.addEventListener("change", render);
    $themeToggle.addEventListener("click", toggleTheme);
    $refreshBtn.addEventListener("click", () => manualRefresh());
    $modalClose.addEventListener("click", closeModal);
    $overlay.addEventListener("click", (e) => { if (e.target === $overlay) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  }

  /* ── Fetch helpers ── */
  async function fetchJSON(url) {
    const resp = await fetch(url, { credentials: "same-origin" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  async function fetchConfig() {
    try { config = await fetchJSON("/api/config"); } catch (e) { console.error("config:", e); }
  }

  async function fetchHosts() {
    try { hosts = await fetchJSON("/api/hosts"); } catch (e) { console.error("hosts:", e); hosts = []; }
  }

  async function fetchHistory() {
    try { historyData = await fetchJSON("/api/history"); } catch (e) { console.error("history:", e); historyData = {}; }
  }

  /* ── Render ── */
  function render() {
    renderSummary();
    renderGrid();
    $serverTime.textContent = config.server_time ? `UTC ${config.server_time}` : "";
  }

  function renderSummary() {
    const counts = { ok: 0, warn: 0, err: 0, offline: 0 };
    hosts.forEach((h) => counts[h.status] = (counts[h.status] || 0) + 1);
    const labels = { ok: "正常", warn: "警告", err: "严重", offline: "离线" };
    $summary.innerHTML = Object.entries(counts)
      .map(([k, v]) => `<span class="summary-chip ${k}" data-filter="${k}">${labels[k]} ${v}</span>`)
      .join("");
    $summary.querySelectorAll(".summary-chip").forEach((el) => {
      el.addEventListener("click", () => {
        $filterStatus.value = el.dataset.filter;
        render();
      });
    });
  }

  function getFilteredSorted() {
    let list = [...hosts];
    const filter = $filterStatus.value;
    if (filter !== "all") list = list.filter((h) => h.status === filter);

    const sort = $sortBy.value;
    const statusOrder = { err: 0, offline: 1, warn: 2, ok: 3 };
    list.sort((a, b) => {
      if (sort === "status") return (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) || a.hostname.localeCompare(b.hostname);
      if (sort === "hostname") return a.hostname.localeCompare(b.hostname);
      if (sort === "cpu") return b.cpu_percent - a.cpu_percent;
      if (sort === "memory") return b.memory_percent - a.memory_percent;
      if (sort === "disk") return b.disk_percent - a.disk_percent;
      return 0;
    });
    return list;
  }

  function renderGrid() {
    sparkPlots.forEach((p) => p.destroy());
    sparkPlots.clear();

    const list = getFilteredSorted();
    if (!list.length) {
      $grid.innerHTML = `<div class="empty-state"><h3>${hosts.length ? "没有匹配的主机" : "暂无巡检数据"}</h3><p>${hosts.length ? "试试切换筛选条件" : "请确认 Agent 已启动并成功上报"}</p></div>`;
      return;
    }

    $grid.innerHTML = list.map((h) => cardHTML(h)).join("");
    list.forEach((h) => renderSparkline(h));
    $grid.querySelectorAll(".card[data-host]").forEach((el) => {
      el.addEventListener("click", () => openDetail(el.dataset.host));
    });
  }

  function cardHTML(h) {
    const th = config.thresholds || {};
    const cpuClass = h.cpu_percent >= (th.cpu_err || 95) ? "high" : h.cpu_percent >= (th.cpu_warn || 80) ? "med" : "";
    const memClass = h.memory_percent >= (th.mem_err || 95) ? "high" : h.memory_percent >= (th.mem_warn || 85) ? "med" : "";
    const diskClass = h.disk_percent >= (th.disk_err || 98) ? "high" : h.disk_percent >= (th.disk_warn || 90) ? "med" : "";
    const ageText = formatAge(h.age_seconds);
    const reasonsPreview = (h.reasons || []).slice(0, 2).join("；");
    const reasonClass = h.status === "err" ? "err" : "";

    return `<div class="card status-${h.status}" data-host="${esc(h.hostname)}">
      <div class="card-header">
        <span class="card-hostname">${esc(h.hostname)}</span>
        <span class="badge ${esc(h.status)}">${esc(h.status_text || h.status)}</span>
      </div>
      <div class="metrics">
        <div class="metric"><div class="metric-value ${cpuClass}">${(h.cpu_percent ?? 0).toFixed(1)}%</div><div class="metric-label">CPU</div></div>
        <div class="metric"><div class="metric-value ${memClass}">${(h.memory_percent ?? 0).toFixed(1)}%</div><div class="metric-label">内存</div></div>
        <div class="metric"><div class="metric-value ${diskClass}">${(h.disk_percent ?? 0).toFixed(1)}%</div><div class="metric-label">磁盘</div></div>
      </div>
      <div class="sparkline-wrap" id="spark-${cssId(h.hostname)}"></div>
      <div class="card-age">${ageText}</div>
      ${reasonsPreview ? `<div class="card-reasons ${reasonClass}">${esc(reasonsPreview)}</div>` : ""}
    </div>`;
  }

  /* ── Sparklines ── */
  function renderSparkline(h) {
    const id = cssId(h.hostname);
    const container = document.getElementById(`spark-${id}`);
    if (!container) return;
    const hd = historyData[h.hostname];
    if (!hd || !hd.t || hd.t.length < 2) return;

    const color = h.status === "err" ? "#f85149" : h.status === "warn" ? "#d29922" : h.status === "offline" ? "#8b949e" : "#3fb950";

    const opts = {
      width: container.clientWidth,
      height: 40,
      cursor: { show: false },
      legend: { show: false },
      axes: [{ show: false }, { show: false }],
      grid: { show: false },
      series: [
        {},
        { stroke: color, width: 1.5, fill: color + "20" },
      ],
    };
    const data = [hd.t, hd.cpu];
    try {
      const plot = new uPlot(opts, data, container);
      sparkPlots.set(h.hostname, plot);
    } catch (e) { /* uPlot may fail on tiny containers */ }
  }

  /* ── Detail Modal ── */
  async function openDetail(hostname) {
    const h = hosts.find((x) => x.hostname === hostname);
    if (!h) return;

    document.getElementById("modal-hostname").textContent = h.hostname;
    const badge = document.getElementById("modal-status-badge");
    badge.className = `badge ${h.status}`;
    badge.textContent = `${h.status_text || h.status} · ${formatAge(h.age_seconds)}`;

    const metrics = [
      { lbl: "CPU", val: `${(h.cpu_percent ?? 0).toFixed(1)}%` },
      { lbl: "内存", val: `${(h.memory_percent ?? 0).toFixed(1)}%` },
      { lbl: "磁盘", val: `${(h.disk_percent ?? 0).toFixed(1)}%` },
      { lbl: "负载 1m", val: h.load_1m?.toFixed(2) ?? "-" },
      { lbl: "服务总数", val: h.services_total ?? "-" },
      { lbl: "失败服务", val: h.services_failed ?? 0 },
      { lbl: "进程数", val: h.process_count ?? "-" },
      { lbl: "SMART", val: h.smart_health || "unknown" },
      { lbl: "上报时间", val: h.reported_at || "-" },
    ];
    document.getElementById("detail-metrics").innerHTML = metrics
      .map((m) => `<div class="detail-item"><div class="val">${esc(String(m.val))}</div><div class="lbl">${esc(m.lbl)}</div></div>`)
      .join("");

    const reasons = h.reasons || [];
    document.getElementById("detail-reasons").innerHTML = reasons.length
      ? reasons.map((r) => `<li class="${h.status === 'err' ? 'critical' : ''}">${esc(r)}</li>`).join("")
      : '<li style="background:none;color:var(--ok)">无告警</li>';

    const ssh = h.ssh_issues || [];
    document.getElementById("detail-ssh").innerHTML = ssh.length
      ? ssh.map((s) => `<li>${esc(s)}</li>`).join("")
      : '<li style="background:none;color:var(--ok)">无安全问题</li>';

    $overlay.classList.add("active");
    document.body.style.overflow = "hidden";

    await renderDetailCharts(hostname);
  }

  function closeModal() {
    $overlay.classList.remove("active");
    document.body.style.overflow = "";
    window._detailPlots?.forEach((p) => p.destroy());
    window._detailPlots = [];
  }

  async function renderDetailCharts(hostname) {
    window._detailPlots?.forEach((p) => p.destroy());
    window._detailPlots = [];

    let hd = historyData[hostname];
    if (!hd || !hd.t || hd.t.length < 2) {
      try {
        const rows = await fetchJSON(`/api/hosts/${encodeURIComponent(hostname)}?hours=${Math.max(1, Math.round(config.history_hours || 6))}`);
        if (rows && rows.length) {
          hd = {
            t: rows.map((r) => Math.floor(new Date(r.created_at.replace(" ", "T") + "Z").getTime() / 1000)),
            cpu: rows.map((r) => r.cpu_percent ?? 0),
            mem: rows.map((r) => r.memory_percent ?? 0),
            disk: rows.map((r) => r.disk_percent ?? 0),
          };
        }
      } catch (e) { console.error("detail history:", e); }
    }
    if (!hd || !hd.t || hd.t.length < 2) return;

    const isDark = document.documentElement.getAttribute("data-theme") !== "light";
    const gridColor = isDark ? "#21262d" : "#eaeef2";
    const textColor = isDark ? "#8b949e" : "#656d76";

    const makeOpts = (containerId, color) => ({
      width: document.getElementById(containerId).clientWidth,
      height: 160,
      cursor: { drag: { x: true, y: false } },
      legend: { show: false },
      axes: [
        { stroke: textColor, grid: { stroke: gridColor } },
        { stroke: textColor, grid: { stroke: gridColor }, min: 0, max: 100 },
      ],
      series: [
        {},
        { stroke: color, width: 2, fill: color + "15" },
      ],
    });

    const charts = [
      { id: "chart-cpu", data: [hd.t, hd.cpu], color: "#58a6ff" },
      { id: "chart-mem", data: [hd.t, hd.mem], color: "#bc8cff" },
      { id: "chart-disk", data: [hd.t, hd.disk], color: "#3fb950" },
    ];

    charts.forEach(({ id, data, color }) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerHTML = "";
      try {
        const plot = new uPlot(makeOpts(id, color), data, el);
        window._detailPlots.push(plot);
      } catch (e) { console.error("chart:", id, e); }
    });
  }

  /* ── Auto-refresh ── */
  function startAutoRefresh() {
    stopAutoRefresh();
    const sec = config.refresh_seconds || 30;
    refreshTimer = setInterval(async () => {
      await Promise.all([fetchHosts(), fetchHistory()]);
      try { const c = await fetchJSON("/api/config"); config = c; } catch (_) {}
      render();
    }, sec * 1000);
  }

  function stopAutoRefresh() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }

  async function manualRefresh() {
    showToast("刷新中…");
    stopAutoRefresh();
    await Promise.all([fetchHosts(), fetchHistory()]);
    try { const c = await fetchJSON("/api/config"); config = c; } catch (_) {}
    render();
    startAutoRefresh();
    showToast("已刷新");
  }

  /* ── Theme ── */
  function loadTheme() {
    const saved = localStorage.getItem("inspect-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("inspect-theme", next);
    renderGrid();
  }

  /* ── Utilities ── */
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function cssId(s) {
    return s.replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function formatAge(seconds) {
    if (seconds == null) return "未知";
    const s = Number(seconds);
    if (s < 60) return `${Math.floor(s)} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }

  function showToast(msg) {
    $toast.textContent = msg;
    $toast.classList.add("show");
    setTimeout(() => $toast.classList.remove("show"), 2000);
  }

  function showSkeletons() {
    $grid.innerHTML = Array.from({ length: 6 }, () => '<div class="skeleton"></div>').join("");
  }

  /* ── Boot ── */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

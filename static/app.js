/* CaddyLog dashboard logic: fetch layer, rendering, window tabs, anomaly
   drill-down and live updates.

   Live-update strategy (mirrors the server design in api.py):
   - primary:  EventSource on /stream — the server pushes an "update" event
               whenever new rows land in the database; EventSource reconnects
               by itself if the stream drops.
   - fallback: if the stream keeps failing (e.g. a proxy that buffers SSE),
               give up after SSE_FAILURE_LIMIT errors and poll every 30 s.
   No build step, no framework — one ES module plus vendored uPlot. */

// ---------- config & state ----------

const POLL_FALLBACK_MS = 30_000; // used only when /stream is unavailable
const SSE_FAILURE_LIMIT = 3;     // consecutive stream errors before falling back

const WINDOW_BUCKETS = {         // shown under the chart title
  "5m": "1-minute buckets", "1h": "5-minute buckets", "1d": "hourly buckets",
  "30d": "daily buckets", "year": "monthly buckets",
};

const state = { window: "1h", path: [] }; // path: [{label, from, to}] drill trail (ms epoch), oldest first

// ---------- tiny helpers ----------

const $ = (sel) => document.querySelector(sel);

const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// API timestamps are naive ISO (local wall-clock time) or may carry a UTC
// offset; either way Date.parse resolves the right absolute instant. Only the
// fractional seconds need trimming — the spec guarantees millisecond digits,
// and DuckDB can emit microseconds.
const parseTs = (t) => {
  const m = t.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$/);
  return Date.parse(m ? `${m[1]}.${m[2].slice(0, 3)}${m[3]}` : t);
};

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function rgba(hex, alpha) { // "#58a6ff" -> "rgba(88,166,255,alpha)" for chart fills
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// Compact axis labels: 0 / 5k / 300k / 1.2M (full Intl formatting is too wide for a widget)
const fmtCompact = (v) => v >= 1e6 ? `${(v / 1e6).toFixed(1).replace(/\.0$/, "")}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : String(Math.round(v));

// ---------- click-to-zoom helpers ----------

// Naive local ISO for the API's from/to params — same wall-clock convention as the
// timestamps /stats returns (parseTs treats them as local time).
const isoLocal = (ms) => {
  const d = new Date(ms), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

// Mirror of api.py's BUCKET_LADDER: [max span ms, bucket duration ms, label].
// A clicked bucket can zoom further iff the child view (ladder on its span) would
// have ≥2 points. Keep in sync with the server.
const LADDER = [
  [10 * 60e3,     60e3,      "per-minute"],
  [2 * 3600e3,    5 * 60e3,  "5-minute buckets"],
  [30 * 3600e3,   3600e3,    "hourly buckets"],
  [45 * 86400e3,  86400e3,   "daily buckets"],
];
const childFor = (spanMs) => {
  for (const [max, dur, label] of LADDER) if (spanMs <= max) return { dur, label };
  return { dur: 28 * 86400e3, label: "monthly buckets" }; // conservative lower bound
};
const zoomable = (spanMs) => spanMs > childFor(spanMs).dur;

// Human-readable label for a [from, to) range — used in breadcrumbs and the header.
function rangeLabel(from, to) {
  const d = new Date(from);
  if (to - from >= 28 * 86400e3) return d.toLocaleString(undefined, { month: "long", year: "numeric" });
  if (to - from >= 24 * 3600e3) return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const hm = (x) => new Date(x).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${hm(from)}–${hm(to)}`;
}

// What the current view is looking at (header label + anomaly drill-down context).
function viewLabel() {
  const top = state.path.at(-1);
  if (top) return rangeLabel(top.from, top.to);
  return state.window === "year" ? String(new Date().getFullYear())
    : { "5m": "last 5 min", "1h": "last hour", "1d": "last 24 h", "30d": "last 30 days" }[state.window];
}

// Idle subtitle under the chart title: bucket size + what a click will do.
function idleSubtitle(stats) {
  const top = state.path.at(-1);
  const label = top ? childFor(top.to - top.from).label : WINDOW_BUCKETS[state.window] ?? "";
  const hint = stats.bucket_ms == null || zoomable(stats.bucket_ms) ? "click to zoom" : "click for raw events";
  return `· ${label} · ${hint}`;
}

// ---------- fetch layer (one thin function per endpoint) ----------

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function statsUrl() {
  const top = state.path.at(-1);
  return top ? `/stats?from=${isoLocal(top.from)}&to=${isoLocal(top.to)}`
             : `/stats?window=${encodeURIComponent(state.window)}`;
}
const fetchStats = () => getJSON(statsUrl());
const fetchAnomalies = () => getJSON("/anomalies");
const fetchStatus = () => getJSON("/status");

function fetchEvents(filter = {}) {
  const p = new URLSearchParams();
  if (filter.from && filter.to) {           // explicit range (finest-grain click)
    p.set("from", filter.from); p.set("to", filter.to);
  } else if (state.path.length) {           // anomaly drill-down: scope to the visible range
    const top = state.path.at(-1);
    p.set("from", isoLocal(top.from)); p.set("to", isoLocal(top.to));
  } else p.set("window", state.window);     // preset mode, as before
  for (const [key, value] of Object.entries(filter))
    if (value != null && value !== "" && key !== "from" && key !== "to") p.set(key, value);
  p.set("limit", "200");
  return getJSON(`/events?${p}`);
}

// ---------- rendering ----------

let chart = null; // uPlot instance (created on first non-empty render)
let lastStats = null; // latest /stats response — hover-leave restore and click decisions read it
// uPlot v1.6.x's hooks.click only fires when the cursor position changed since the last
// event — a plain "hover, then click" (the normal interaction) commits its position on
// mousemove and gets swallowed. So listen on the container directly instead; the hovered
// bucket index is read from self.cursor at click time (set by the setCursor hook above).
let chartClickBound = false;
function bindChartClick() {
  if (chartClickBound) return;
  chartClickBound = true;
  $("#chart").addEventListener("click", (e) => {
    if (!chart || e.target.closest(".u-over") == null) return; // plot area only, not the axes
    onChartClick(chart);
  });
}
// uPlot v1.6.x does NOT auto-size from its container (no clientWidth/ResizeObserver
// in the build) — it only sizes what we tell it via width/height options or setSize().
// So: seed explicit dimensions at creation (see chartOptions) and keep them in sync
// on any reflow (window resize, responsive breakpoint) through this observer.
new ResizeObserver(() => {
  if (chart) chart.setSize({ width: $("#chart").clientWidth, height: $("#chart").clientHeight });
}).observe($("#chart"));

function chartOptions() {
  const accent = cssVar("--accent");
  return {
    // explicit dimensions — uPlot v1.6.x has no container auto-sizing (see above)
    width: $("#chart").clientWidth,
    height: $("#chart").clientHeight,
    // uPlot v1.6.x API notes (differ from the widely-copied older examples):
    // - scale options live under `scales` (plural), not `scale`
    // - timestamps are in SECONDS by default; our data is milliseconds -> ms: 1
    scales: { x: { time: true }, y: { range: [0, null] } },
    ms: 1,
    padding: [12, 14, 4, 0],
    legend: { show: false },
    cursor: { drag: false, points: { size: 5 } },
    axes: [
      { stroke: cssVar("--muted"), grid: { stroke: cssVar("--border") },
        ticks: { stroke: cssVar("--border") }, font: "12px ui-monospace, monospace" },
      // y axis: visible count labels — without them the chart is unreadable (what is a "high" point?)
      { stroke: cssVar("--muted"), grid: { stroke: cssVar("--border") },
        ticks: { stroke: cssVar("--border") }, font: "12px ui-monospace, monospace",
        values: (_u, splits) => splits.map(fmtCompact) }, // v1.6.x: values gets the split array, returns labels
    ],
    // Live value readout in the card header (uPlot hook API): hovering shows the bucket's
    // time + exact count; leaving restores the plain bucket label.
    hooks: {
      setCursor: [(u) => {
        const sub = $("#chart-sub");
        if (!chart || u.cursor.idx == null || u.cursor.idx >= u.data[1].length) {
          sub.textContent = lastStats ? idleSubtitle(lastStats) : "";
          return;
        }
        const when = new Date(u.data[0][u.cursor.idx]).toLocaleString();
        sub.textContent = `${when} · ${Math.round(u.data[1][u.cursor.idx]).toLocaleString()}`;
      }],
    },
    // normal mode: series[0] = x/time column (null), series[1] = the value series
    series: [null, { label: "requests", stroke: accent, width: 2, fill: rgba(accent, 0.12), points: { show: false } }],
  };
}

function renderChart(stats) {
  const xs = stats.timeseries.map(([t]) => parseTs(t));
  const ys = stats.timeseries.map(([, count]) => count);
  lastStats = stats;
  $("#range-label").textContent = viewLabel();
  $("#chart-sub").textContent = idleSubtitle(stats);

  if (xs.length === 0) { // nothing in this window: show placeholder, drop the chart
    if (chart) { chart.destroy(); chart = null; }
    $("#chart-empty").classList.remove("hidden");
    return;
  }
  $("#chart-empty").classList.add("hidden");
  // pointer cursor while a click would zoom in (finest level: raw events instead)
  const clickable = xs.length > 1 && (stats.bucket_ms == null || zoomable(stats.bucket_ms));
  $("#chart").classList.toggle("clickable", clickable);
  if (!chart) chart = new uPlot(chartOptions(), [xs, ys], $("#chart"));
  else chart.setData([xs, ys]); // v1.6.x: .update() was removed — setData() is the API
  bindChartClick();
}

const STATUS_GROUPS = [["2xx", "var(--success)"], ["3xx", "var(--muted)"],
                       ["4xx", "var(--warning)"], ["5xx", "var(--error)"]];

function renderStatusClasses(breakdown) {
  const sums = Object.fromEntries(STATUS_GROUPS.map(([group]) => [group, 0]));
  for (const [status, count] of Object.entries(breakdown)) {
    const group = `${Math.floor(Number(status) / 100)}xx`;
    if (group in sums) sums[group] += count;
  }
  $("#status-classes").innerHTML = STATUS_GROUPS.map(([group, color]) => `
    <li><span class="dot" style="background:${color}"></span>${group}
        <span class="count">${sums[group].toLocaleString()}</span></li>`).join("");
}

function renderHosts(top) {
  $("#top-hosts").innerHTML = top.length ? top.map(({ host, count }) => `
    <li><span class="host-name" title="${esc(host ?? "(no host)")}">${esc(host ?? "(no host)")}</span>
        <span class="count">${count.toLocaleString()}</span></li>`).join("")
    : `<li class="muted">none</li>`;
}

function renderAnomalies(findings) {
  const badge = $("#anomaly-count");
  badge.textContent = findings.length;
  badge.className = `badge ${findings.length ? "badge-warn" : "badge-zero"}`;

  const list = $("#anomaly-list");
  if (!findings.length) { list.innerHTML = `<li class="muted">no findings</li>`; return; }

  list.innerHTML = findings.map((a, i) => `
    <li class="finding" data-i="${i}" tabindex="0" role="button">
      <span class="dot dot-${esc(a.kind)}"></span>
      <span class="kind">${esc(a.kind)}</span>
      <span>${esc(a.entity ?? "—")} — ${esc(a.reason)}</span>
    </li>`).join("");

  list.querySelectorAll(".finding").forEach((el) => {
    const open = () => openDrilldown(findings[Number(el.dataset.i)]);
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

function renderFreshness(status, latestEvent) {
  $("#status-dot").className = `dot ${status.ok ? "dot-ok" : "dot-warn"}`;
  const through = latestEvent ? new Date(parseTs(latestEvent)).toLocaleTimeString() : "—";
  const word = status.anomalies === 1 ? "anomaly" : "anomalies";
  $("#freshness-text").textContent =
    `${status.ok ? "ok" : "attention"} · data through ${through} · ${status.anomalies} ${word}`;
}

// ---------- drill-down (raw rows for a clicked anomaly) ----------

function drillFilterFor(finding) {
  const filter = {};
  if (finding.kind === "server_errors") { // entity is the host, may be null
    if (finding.entity) filter.host = finding.entity;
    filter.min_status = 500;
  } else if (finding.entity) {            // hot_ip / volume_outlier: entity is an IP
    filter.ip = finding.entity;
  }
  return filter;
}

function describeFilter(filter, kind) {
  const parts = [];
  if (filter.ip) parts.push(`ip ${filter.ip}`);
  if (filter.host) parts.push(`host ${filter.host}`);
  if (filter.min_status) parts.push(`status ≥ ${filter.min_status}`);
  return `${parts.join(" · ")} · ${viewLabel()} (${kind})`;
}

function statusClass(status) { // CSS class for the status cell: st-2xx … st-5xx
  if (status == null) return "";
  const cls = `${Math.min(Math.floor(Number(status) / 100), 5)}xx`;
  return cls in Object.fromEntries(STATUS_GROUPS.map(([g]) => [g, true])) ? `st-${cls}` : "";
}

function renderEvents(resp) {
  $("#event-rows").innerHTML = resp.events.length ? resp.events.map((e) => `
    <tr>
      <td>${new Date(parseTs(e.event_ts)).toLocaleTimeString()}</td>
      <td>${esc(e.remote_ip ?? "—")}</td>
      <td>${esc(e.host ?? "—")}</td>
      <td>${esc(e.method ?? "—")}</td>
      <td title="${esc(e.uri_path ?? "")}">${esc((e.uri_path ?? "").slice(0, 60))}</td>
      <td class="${statusClass(e.status)}">${e.status ?? "—"}</td>
      <td>${e.response_size != null ? e.response_size.toLocaleString() : "—"}</td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="muted">no matching rows</td></tr>`;
}

async function openDrilldown(finding) {
  const filter = drillFilterFor(finding);
  if (!Object.keys(filter).length) return; // nothing to narrow on — stay put
  $("#drilldown").classList.remove("hidden");
  $("#drilldown-query").textContent = describeFilter(filter, finding.kind);
  try { renderEvents(await fetchEvents(filter)); } catch (err) { console.error(err); }
}

function closeDrilldown() { $("#drilldown").classList.add("hidden"); }

// ---------- click-to-zoom (drill path + breadcrumbs) ----------

const nextMonthStart = (ms) => { const d = new Date(ms); return new Date(d.getFullYear(), d.getMonth() + 1, 1).getTime(); };

function onChartClick(u) {
  const i = u.cursor.idx;
  if (!chart || i == null || i >= chart.data[0].length) return;
  const xs = chart.data[0];
  const from = xs[i];
  // last (in-progress) bucket has no successor: extend by one nominal step
  const to = i + 1 < xs.length ? xs[i + 1] : from + (lastStats.bucket_ms ?? nextMonthStart(from));
  if (!zoomable(to - from)) { openRangeEvents(from, to); return; }   // finest level → raw rows
  state.path.push({ label: rangeLabel(from, to), from, to });
  renderCrumbs();
  refreshAll();
}

async function openRangeEvents(from, to) {
  $("#drilldown").classList.remove("hidden");
  $("#drilldown-query").textContent = `raw events · ${rangeLabel(from, to)}`;
  try { renderEvents(await fetchEvents({ from: isoLocal(from), to: isoLocal(to) })); }
  catch (err) { console.error(err); }
}

function renderCrumbs() {
  const box = $("#crumbs");
  if (!state.path.length) { box.innerHTML = ""; return; }
  box.innerHTML = state.path.map((p, i) =>
    `<button class="crumb" data-i="${i}" title="jump to ${esc(p.label)}">${esc(p.label)}</button>`).join('<span class="crumb-sep">›</span>') +
    '<span class="crumb-sep">›</span><button class="crumb crumb-home" id="crumb-reset" title="back to the default view">⌂ now</button>';
  box.querySelectorAll(".crumb").forEach((el) => el.addEventListener("click", () => {
    if (el.id === "crumb-reset") state.path = [];
    else state.path.length = Number(el.dataset.i) + 1;   // keep this level and everything above it
    renderCrumbs();
    refreshAll();
  }));
}

// ---------- refresh & live updates ----------

async function refreshAll() {
  try {
    const [stats, findings, status] = await Promise.all([fetchStats(), fetchAnomalies(), fetchStatus()]);
    renderChart(stats);
    $("#total-count").textContent = stats.total_requests.toLocaleString();
    renderStatusClasses(stats.status_breakdown);
    renderHosts(stats.top_hosts);
    renderAnomalies(findings);
    renderFreshness(status, stats.latest_event);
  } catch (err) {
    console.error("refresh failed:", err);
    $("#status-dot").className = "dot dot-warn";
    $("#freshness-text").textContent = "refresh failed — see console";
  }
}

function startLiveUpdates() {
  let failures = 0;
  const es = new EventSource("/stream");
  es.addEventListener("open", () => { failures = 0; });
  es.addEventListener("update", () => refreshAll());
  es.onerror = () => {
    // EventSource retries on its own. If it keeps failing (e.g. a proxy that
    // buffers SSE), give up and fall back to plain polling — the dashboard
    // just updates more slowly, nothing breaks.
    if (++failures >= SSE_FAILURE_LIMIT) {
      es.close();
      setInterval(refreshAll, POLL_FALLBACK_MS);
    }
  };
}

// ---------- controls & init ----------

function wireControls() {
  $("#btn-year").textContent = new Date().getFullYear(); // 'year' tab = current calendar year; picker comes later
  $("#tabs").querySelectorAll("button[data-window]").forEach((btn) => btn.addEventListener("click", () => {
    if (state.window === btn.dataset.window && !state.path.length) return;
    state.window = btn.dataset.window;
    state.path = [];                       // presets are always now-anchored: clear the drill trail
    $("#tabs").querySelectorAll("button[data-window]")
      .forEach((b) => b.classList.toggle("active", b === btn));
    renderCrumbs();
    refreshAll();
  }));
  $("#refresh-btn").addEventListener("click", () => refreshAll());
  $("#drilldown-close").addEventListener("click", closeDrilldown);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrilldown(); });
}

wireControls();
refreshAll().then(startLiveUpdates); // first paint, then start the push channel

"""管理后台单页（`GET /admin/ui`）。

以 Python 常量而非独立 `.html` 存在：`pyproject.toml` 没有 package-data，静态文件
不会被打包进去。页面自包含、零外部请求，密钥只存 sessionStorage。

请求纪律迁就 admin 限流桶（默认 rate_per_minute=30、burst=10、concurrency=2）：
客户端并发上限 2、面板懒加载、默认不自动刷新、429 时读 Retry-After 退避。

页面里唯一的 `http://` 字面量是 `createElementNS` 需要的 SVG 命名空间常量，
它不产生任何网络请求；除此之外没有 `src=` / `<link>` / `url(http…)` 引用。
"""

ADMIN_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>超天酱 · 管理后台</title>
<style>
:root {
  --bg: #10121a; --panel: #191c27; --line: #2b3040; --text: #e6e8f0;
  --dim: #939ab5; --accent: #7cc4ff; --warn: #ffcf6b; --bad: #ff8a8a;
  --ok: #8ce99a;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
}
header {
  position: sticky; top: 0; z-index: 5; background: #141722;
  border-bottom: 1px solid var(--line); padding: 10px 16px;
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
}
header h1 { font-size: 15px; margin: 0 12px 0 0; font-weight: 600; }
input, select, button {
  background: #0d0f16; color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 10px; font: inherit; font-size: 13px;
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
button.primary { background: #1d3550; border-color: #2f5b85; }
button.danger { border-color: #6b2f2f; }
button:disabled { opacity: .45; cursor: not-allowed; }
#status { font-size: 12px; color: var(--dim); margin-left: auto; }
#status.ok { color: var(--ok); } #status.bad { color: var(--bad); }
main { display: flex; align-items: flex-start; }
nav {
  width: 190px; flex: 0 0 190px; padding: 12px 8px; position: sticky; top: 52px;
  max-height: calc(100vh - 52px); overflow: auto;
}
nav .grp { color: var(--dim); font-size: 11px; margin: 12px 8px 4px; letter-spacing: .08em; }
nav a {
  display: block; padding: 5px 10px; border-radius: 6px; color: var(--text);
  text-decoration: none; font-size: 13px;
}
nav a:hover { background: #20242f; }
section { flex: 1; padding: 12px 16px 60px; min-width: 0; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 12px; overflow: hidden;
}
.card > h2 {
  font-size: 13px; margin: 0; padding: 10px 12px; cursor: pointer;
  display: flex; gap: 8px; align-items: center; font-weight: 600;
}
.card > h2 .path { color: var(--dim); font-weight: 400; font-size: 11px; }
.card > h2 .caret { color: var(--dim); font-size: 11px; }
.body { padding: 0 12px 12px; display: none; }
.card.open .body { display: block; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.row input { width: 120px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid #23273400; }
th { color: var(--dim); font-weight: 500; border-bottom: 1px solid var(--line); }
tbody tr:nth-child(odd) { background: #1d212c; }
th:first-child, td:first-child { text-align: left; }
pre {
  background: #0d0f16; border: 1px solid var(--line); border-radius: 8px;
  padding: 10px; overflow: auto; max-height: 420px; font-size: 12px;
  white-space: pre-wrap; word-break: break-word; margin: 0;
}
.tag {
  font-size: 10.5px; padding: 1px 6px; border-radius: 10px; border: 1px solid var(--line);
  color: var(--dim); margin-left: 4px;
}
.tag.warn { color: var(--warn); border-color: #5a4a24; }
.tag.bad { color: var(--bad); border-color: #5a2828; }
.kpi { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.kpi div {
  background: #1d212c; border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 12px; min-width: 118px;
}
.kpi b { display: block; font-size: 17px; font-weight: 600; }
.kpi span { color: var(--dim); font-size: 11px; }
.muted { color: var(--dim); }
.err { color: var(--bad); }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; }
svg { display: block; width: 100%; height: 190px; }
.legend { font-size: 11px; color: var(--dim); margin-top: 4px; }
.legend i { display: inline-block; width: 9px; height: 9px; margin: 0 4px 0 10px; }
</style>
</head>
<body>
<header>
  <h1>超天酱 · 管理后台</h1>
  <input id="key" type="password" placeholder="粘贴 ADMIN__API_KEY" autocomplete="off" size="26">
  <button id="save" class="primary">连接</button>
  <button id="forget">清除密钥</button>
  <label class="muted" style="font-size:12px">
    <input id="auto" type="checkbox" style="width:auto"> 自动刷新
  </label>
  <select id="autoSec">
    <option value="30">30s</option><option value="60" selected>60s</option>
    <option value="180">180s</option>
  </select>
  <span id="status">未连接</span>
</header>
<main>
  <nav id="nav"></nav>
  <section id="panels"></section>
</main>
<script>
"use strict";
// ---------------------------------------------------------------- 密钥
// 只存 sessionStorage：关标签即失效，不写 localStorage、不写 cookie、不进 URL。
var KEY_STORE = "kangel.admin.key";
function getKey() { try { return sessionStorage.getItem(KEY_STORE) || ""; } catch (e) { return ""; } }
function setKey(v) { try { v ? sessionStorage.setItem(KEY_STORE, v) : sessionStorage.removeItem(KEY_STORE); } catch (e) {} }

// ------------------------------------------------- 请求队列（并发上限 2）
// admin 桶默认 burst=10 / concurrency=2，客户端也压到 2 才不会自己把自己限流。
var MAX_INFLIGHT = 2, inflight = 0, waiting = [], backoffUntil = 0;

function schedule() {
  while (inflight < MAX_INFLIGHT && waiting.length) {
    var job = waiting.shift();
    inflight++;
    job().catch(function () {}).then(function () { inflight--; schedule(); });
  }
}

function api(path, opts) {
  return new Promise(function (resolve, reject) {
    waiting.push(function () {
      var wait = Math.max(0, backoffUntil - Date.now());
      return new Promise(function (go) { setTimeout(go, wait); }).then(function () {
        var o = opts || {};
        return fetch(path, {
          method: o.method || "GET",
          headers: Object.assign({ "x-admin-key": getKey() },
            o.body ? { "Content-Type": "application/json" } : {}),
          body: o.body ? JSON.stringify(o.body) : undefined,
          cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer"
        });
      }).then(function (res) {
        if (res.status === 429) {
          var ra = parseInt(res.headers.get("Retry-After") || "5", 10);
          if (!isFinite(ra) || ra < 1) ra = 5;
          backoffUntil = Date.now() + ra * 1000;
          setStatus("已被限流，" + ra + "s 后可重试（请少点几个面板）", "bad");
          throw new Error("429 限流：" + ra + "s 后重试");
        }
        if (res.status === 401 || res.status === 403) {
          setStatus("密钥无效（" + res.status + "）", "bad");
          throw new Error("鉴权失败：" + res.status);
        }
        if (res.status === 404) throw new Error("404：接口未启用或路径不存在");
        return res.text().then(function (t) {
          if (!res.ok) throw new Error("HTTP " + res.status + " " + t.slice(0, 200));
          try { return t ? JSON.parse(t) : null; } catch (e) { return t; }
        });
      }).then(resolve, reject);
    });
    schedule();
  });
}

function setStatus(text, cls) {
  var el = document.getElementById("status");
  el.textContent = text; el.className = cls || "";
}

// ---------------------------------------------------------------- 工具
function el(tag, attrs, kids) {
  var n = document.createElement(tag);
  if (attrs) Object.keys(attrs).forEach(function (k) {
    if (k === "class") n.className = attrs[k];
    else if (k === "text") n.textContent = attrs[k];
    else n.setAttribute(k, attrs[k]);
  });
  (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
  return n;
}
function num(v) {
  if (v === null || v === undefined || v === "") return "-";
  if (typeof v !== "number") return String(v);
  return v.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}
function table(cols, rows) {
  var head = el("tr", null, cols.map(function (c) { return el("th", { text: c.label }); }));
  var body = rows.map(function (r) {
    return el("tr", null, cols.map(function (c) {
      var td = el("td");
      var v = c.get ? c.get(r) : r[c.field];
      if (v instanceof Node) td.appendChild(v); else td.textContent = num(v);
      return td;
    }));
  });
  return el("table", null, [el("thead", null, [head]), el("tbody", null, body)]);
}
function badges(row) {
  var wrap = el("span");
  if (row.fully_priced === false) wrap.appendChild(el("span", { class: "tag warn", text: "未配价" }));
  if (row.usage_missing_calls > 0) {
    wrap.appendChild(el("span", { class: "tag warn", text: "未上报 " + row.usage_missing_calls }));
  }
  if (row.failed_calls > 0) {
    wrap.appendChild(el("span", { class: "tag bad", text: "失败 " + row.failed_calls }));
  }
  return wrap;
}

// ------------------------------------------------------ 内联 SVG 图（零依赖）
function chart(days, currency) {
  var W = 900, H = 190, pad = 30, n = days.length || 1;
  var maxTok = Math.max.apply(null, days.map(function (d) { return d.total_tokens; }).concat([1]));
  var maxCost = Math.max.apply(null, days.map(function (d) { return d.cost_amount || 0; }).concat([0.0001]));
  var bw = (W - pad * 2) / n, svgNS = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("preserveAspectRatio", "none");
  function mk(tag, attrs) {
    var e = document.createElementNS(svgNS, tag);
    Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }
  svg.appendChild(mk("line", { x1: pad, y1: H - 20, x2: W - pad, y2: H - 20, stroke: "#2b3040" }));
  var pts = [];
  days.forEach(function (d, i) {
    var h = Math.round((H - 46) * d.total_tokens / maxTok);
    var x = pad + i * bw, y = H - 20 - h;
    var bar = mk("rect", {
      x: x + bw * 0.15, y: y, width: Math.max(1, bw * 0.7), height: Math.max(0, h),
      fill: d.fully_priced === false ? "#ffcf6b" : "#4b8fd0", rx: 2
    });
    var t = document.createElementNS(svgNS, "title");
    t.textContent = d.day + "  " + num(d.total_tokens) + " tokens" +
      (d.cost_amount ? "  " + num(d.cost_amount) + " " + (currency || "") : "") +
      (d.fully_priced === false ? "  (含未配价模型)" : "");
    bar.appendChild(t);
    svg.appendChild(bar);
    pts.push([x + bw / 2, H - 20 - Math.round((H - 46) * (d.cost_amount || 0) / maxCost)]);
    if (n <= 16 || i % 3 === 0) {
      var lb = mk("text", {
        x: x + bw / 2, y: H - 6, fill: "#939ab5", "font-size": "9", "text-anchor": "middle"
      });
      lb.textContent = d.day.slice(5);
      svg.appendChild(lb);
    }
  });
  if (maxCost > 0.0001) {
    svg.appendChild(mk("polyline", {
      points: pts.map(function (p) { return p[0] + "," + p[1]; }).join(" "),
      fill: "none", stroke: "#8ce99a", "stroke-width": "1.6"
    }));
  }
  return svg;
}

// ---------------------------------------------------------------- Token 面板
function renderTokenAudit(box) {
  box.textContent = "";
  var ctl = el("div", { class: "row" });
  var sel = el("select");
  [7, 14, 30, 60].forEach(function (d) {
    sel.appendChild(el("option", { value: String(d), text: "最近 " + d + " 天" }));
  });
  sel.value = "14";
  var btn = el("button", { class: "primary", text: "刷新" });
  ctl.appendChild(sel); ctl.appendChild(btn);
  var out = el("div", { class: "muted", text: "点「刷新」加载" });
  box.appendChild(ctl); box.appendChild(out);

  function load() {
    out.textContent = "加载中…";
    var days = parseInt(sel.value, 10);
    Promise.all([
      api("/admin/tokens/daily?days=" + days),
      api("/admin/tokens/breakdown?days=" + days)
    ]).then(function (r) {
      var daily = r[0], bd = r[1], cur = daily.currency || "";
      out.textContent = "";
      var t = daily.totals;
      var kpi = el("div", { class: "kpi" });
      [
        ["总 token", num(t.total_tokens)],
        ["输入 / 输出", num(t.input_tokens) + " / " + num(t.output_tokens)],
        ["缓存输入", num(t.cached_input_tokens)],
        ["调用次数", num(t.calls) + (t.failed_calls ? "（失败 " + t.failed_calls + "）" : "")],
        ["折算花费", daily.pricing_configured ? num(t.cost_amount) + " " + cur : "未配价目表"],
        ["未计价 token", num(t.unpriced_tokens)],
        ["未上报用量", num(t.usage_missing_calls) + " 次"]
      ].forEach(function (p) {
        kpi.appendChild(el("div", null, [
          el("b", { text: String(p[1]) }), el("span", { text: p[0] })
        ]));
      });
      out.appendChild(kpi);
      out.appendChild(el("div", { class: "muted",
        text: daily.start_day + " ~ " + daily.end_day + "（时区 " + daily.timezone + "）" }));
      out.appendChild(chart(daily.days, cur));
      var lg = el("div", { class: "legend" });
      lg.innerHTML = '<i style="background:#4b8fd0"></i>每日总 token' +
        '<i style="background:#ffcf6b"></i>含未配价模型' +
        '<i style="background:#8ce99a"></i>折算花费';
      out.appendChild(lg);

      var cols = [
        { label: "日期", field: "day" },
        { label: "调用", field: "calls" },
        { label: "输入", field: "input_tokens" },
        { label: "输出", field: "output_tokens" },
        { label: "缓存", field: "cached_input_tokens" },
        { label: "总计", field: "total_tokens" },
        { label: "花费(" + (cur || "-") + ")", field: "cost_amount" },
        { label: "标记", get: badges }
      ];
      out.appendChild(table(cols, daily.days.slice().reverse()));

      var grid = el("div", { class: "grid3" });
      [["by_role", "按角色"], ["by_provider", "按供应商"], ["by_model", "按模型"]].forEach(function (g) {
        var gcols = [
          { label: g[1], field: "key" },
          { label: "调用", field: "calls" },
          { label: "总 token", field: "total_tokens" },
          { label: "花费", field: "cost_amount" },
          { label: "均延迟ms", field: "avg_latency_ms" },
          { label: "标记", get: badges }
        ];
        grid.appendChild(el("div", null, [table(gcols, bd[g[0]])]));
      });
      out.appendChild(grid);
    }).catch(function (e) {
      out.textContent = ""; out.appendChild(el("div", { class: "err", text: String(e.message || e) }));
    });
  }
  btn.onclick = load;
  box.__reload = load;
  load();
}

function renderTokenRecords(box) {
  box.textContent = "";
  var ctl = el("div", { class: "row" });
  var day = el("input", { placeholder: "YYYY-MM-DD" });
  var role = el("input", { placeholder: "role（可空）" });
  var st = el("select");
  [["", "全部状态"], ["success", "success"], ["failed", "failed"]].forEach(function (p) {
    st.appendChild(el("option", { value: p[0], text: p[1] }));
  });
  var btn = el("button", { class: "primary", text: "查询" });
  var prev = el("button", { text: "上一页" }), next = el("button", { text: "下一页" });
  [day, role, st, btn, prev, next].forEach(function (n) { ctl.appendChild(n); });
  var out = el("div", { class: "muted", text: "点「查询」加载" });
  box.appendChild(ctl); box.appendChild(out);
  var offset = 0, limit = 50;

  function load() {
    out.textContent = "加载中…";
    var q = ["limit=" + limit, "offset=" + offset];
    if (/^\\d{4}-\\d{2}-\\d{2}$/.test(day.value.trim())) q.push("day=" + day.value.trim());
    if (role.value.trim()) q.push("role=" + encodeURIComponent(role.value.trim()));
    if (st.value) q.push("status=" + st.value);
    api("/admin/tokens/records?" + q.join("&")).then(function (r) {
      out.textContent = "";
      prev.disabled = offset <= 0;
      next.disabled = offset + limit >= r.total;
      out.appendChild(el("div", { class: "muted", text:
        "共 " + r.total + " 条，当前 " + (offset + 1) + "-" + Math.min(offset + limit, r.total) +
        "；明细保留 " + r.detail_retention_days + " 天" +
        (r.detail_enabled ? "" : "（明细已关闭，只有每日聚合）") }));
      out.appendChild(table([
        { label: "时间(UTC)", get: function (x) { return x.created_at.replace("T", " ").slice(0, 19); } },
        { label: "role", field: "role" }, { label: "provider", field: "provider" },
        { label: "model", field: "model" },
        { label: "状态", get: function (x) {
          return el("span", { class: x.status === "success" ? "" : "err", text: x.status }); } },
        { label: "输入", field: "input_tokens" }, { label: "输出", field: "output_tokens" },
        { label: "总计", field: "total_tokens" },
        { label: "花费", get: function (x) {
          return x.cost_amount === null ? "未配价" : x.cost_amount; } },
        { label: "延迟ms", field: "latency_ms" },
        { label: "备注", get: function (x) {
          return x.error_kind || (x.usage_reported ? "" : "未上报用量"); } }
      ], r.records));
    }).catch(function (e) {
      out.textContent = ""; out.appendChild(el("div", { class: "err", text: String(e.message || e) }));
    });
  }
  btn.onclick = function () { offset = 0; load(); };
  prev.onclick = function () { offset = Math.max(0, offset - limit); load(); };
  next.onclick = function () { offset += limit; load(); };
  box.__reload = load;
}

// ------------------------------------------------------------ 通用渲染器
function renderValue(v) {
  if (v === null || v === undefined) return el("span", { class: "muted", text: "-" });
  if (Array.isArray(v)) {
    if (v.length && typeof v[0] === "object" && v[0] !== null) {
      var keys = Object.keys(v[0]).slice(0, 12);
      return table(keys.map(function (k) {
        return { label: k, get: function (r) {
          var x = r[k];
          return (x !== null && typeof x === "object") ? JSON.stringify(x) : x;
        } };
      }), v.slice(0, 200));
    }
    return el("pre", { text: JSON.stringify(v, null, 2) });
  }
  if (typeof v === "object") {
    var flat = [], deep = {};
    Object.keys(v).forEach(function (k) {
      var x = v[k];
      if (x !== null && typeof x === "object") deep[k] = x;
      else flat.push({ k: k, v: x });
    });
    var wrap = el("div");
    if (flat.length) {
      wrap.appendChild(table([{ label: "字段", field: "k" }, { label: "值", field: "v" }], flat));
    }
    if (Object.keys(deep).length) wrap.appendChild(el("pre", { text: JSON.stringify(deep, null, 2) }));
    return wrap;
  }
  return el("pre", { text: String(v) });
}

// PANELS 是数据驱动注册表：以后加接口只加一行。
// danger 项一律二次确认；PUT /config、POST /persona/reset 刻意标 disabled。
var PANELS = [
  { group: "概览", title: "总览快照", path: "/admin/overview",
    note: "一次请求拿全部常用只读数据，避免开屏打爆 admin 限流桶" },
  { group: "Token 审计", title: "每日用量与花费", path: "/admin/tokens/daily", custom: renderTokenAudit },
  { group: "Token 审计", title: "逐次调用明细", path: "/admin/tokens/records", custom: renderTokenRecords },
  { group: "Token 审计", title: "记账器健康度与价目覆盖", path: "/admin/tokens/stats" },

  { group: "SC", title: "SC 统计", path: "/admin/sc/stats" },
  { group: "SC", title: "SC 配置（公开口径）", path: "/sc/config" },

  { group: "弹幕", title: "弹幕池状态", path: "/danmaku/pool" },
  { group: "弹幕", title: "候选选择器统计", path: "/danmaku/selector/stats" },
  { group: "弹幕", title: "连接信息", path: "/connections" },

  { group: "人格与情绪", title: "人格状态", path: "/persona/state" },
  { group: "人格与情绪", title: "事件流水线调试", path: "/persona/events/debug" },
  { group: "人格与情绪", title: "工作记忆 Prompt RAM", path: "/admin/prompt-ram" },
  { group: "人格与情绪", title: "影响分析调试信息", path: "/persona/impact/debug" },
  { group: "人格与情绪", title: "影响分析历史", path: "/persona/impact/history",
    params: [{ name: "limit", value: "20" }] },
  { group: "人格与情绪", title: "情绪列表", path: "/emotion/list" },
  { group: "人格与情绪", title: "情绪统计", path: "/emotion/stats" },
  { group: "人格与情绪", title: "情绪随机度（读）", path: "/emotion/randomness" },
  { group: "人格与情绪", title: "情绪单项信息", path: "/emotion/info/{emotion_name}",
    params: [{ name: "emotion_name", value: "", inPath: true }] },
  { group: "人格与情绪", title: "设置情绪随机度", path: "/emotion/randomness", method: "POST",
    danger: true, params: [{ name: "randomness", value: "0.3" }],
    confirm: "将修改情绪随机强度（可逆，改回原值即可）" },
  { group: "人格与情绪", title: "重置情绪历史", path: "/emotion/reset-history", method: "POST",
    danger: true, confirm: "将清空情绪使用历史（不影响人格与记忆）" },
  { group: "人格与情绪", title: "手动选择情绪", path: "/emotion/select", method: "POST",
    danger: true, params: [
      { name: "mood", value: "0.5" }, { name: "stress", value: "0.3" },
      { name: "darkness", value: "0.2" }, { name: "count", value: "2" }],
    confirm: "将按给定人格数值试算一次情绪选择（只读性质，但走 POST）" },
  { group: "人格与情绪", title: "影响分析调试模式", path: "/persona/impact/debug-mode", method: "POST",
    danger: true, params: [{ name: "enabled", value: "true" }],
    confirm: "将切换影响分析调试模式（可逆）" },
  { group: "人格与情绪", title: "手动分析弹幕影响", path: "/persona/impact/analyze", method: "POST",
    danger: true, params: [{ name: "content", value: "", inBody: true }],
    confirm: "将对给定文本跑一次影响分析（会改动人格数值）" },
  { group: "人格与情绪", title: "重置人格", path: "/persona/reset", method: "POST",
    disabled: "不可逆，请用 curl 执行" },

  { group: "记忆", title: "弹幕记忆统计", path: "/memory/stats" },
  { group: "记忆", title: "记忆上下文", path: "/memory/context", params: [{ name: "limit", value: "10" }] },
  { group: "记忆", title: "群体讨论", path: "/memory/group-discussion",
    params: [{ name: "topic", value: "" }] },
  { group: "记忆", title: "弹幕对人格的影响", path: "/memory/persona-impact" },
  { group: "记忆", title: "情节记忆统计", path: "/memory/episodic/stats" },

  { group: "直播", title: "直播元信息", path: "/stream/metadata" },
  { group: "直播", title: "元信息推送统计", path: "/stream/metadata/stats" },
  { group: "直播", title: "活动记录", path: "/stream/activities", params: [{ name: "limit", value: "20" }] },
  { group: "直播", title: "心情推送统计", path: "/mood/pusher/stats" },

  { group: "安全与限流", title: "运行指标", path: "/admin/security/stats" },
  { group: "安全与限流", title: "端到端时序追踪", path: "/admin/timing-trace",
    note: "每条弹幕的检查点序列；attempt 与逻辑耗时之差就是回退/等待成本" },
  { group: "安全与限流", title: "表情统计", path: "/admin/emotes/stats" },
  { group: "安全与限流", title: "表情配置", path: "/emotes/config" },
  { group: "安全与限流", title: "审核统计", path: "/admin/moderation/stats" },

  { group: "赞助", title: "赞助同步健康度", path: "/admin/sponsor/stats" },
  { group: "赞助", title: "赞助名单（公开口径）", path: "/sponsors" },
  { group: "赞助", title: "资金透明概览", path: "/sponsor/transparency" },
  { group: "赞助", title: "资金收入同步状态", path: "/admin/sponsor/finance/stats" },
  { group: "赞助", title: "手动同步收入", path: "/admin/sponsor/finance/sync", method: "POST" },
  { group: "赞助", title: "支出记录", path: "/admin/sponsor/expenses", params: [{ name: "include_void", value: "true" }] },
  { group: "赞助", title: "新增支出", path: "/admin/sponsor/expenses", method: "POST", danger: true,
    params: [
      { name: "month", value: "2026-09", inBody: true },
      { name: "amount_cents", value: "0", inBody: true, type: "number" },
      { name: "category", value: "ai_api", inBody: true },
      { name: "title", value: "", inBody: true },
      { name: "public_note", value: "", inBody: true },
    ], confirm: "将新增一条 active 支出记录；金额使用整数分，错误后请作废。" },
  { group: "赞助", title: "编辑支出", path: "/admin/sponsor/expenses/{entry_id}", method: "PUT", danger: true,
    params: [
      { name: "entry_id", value: "", inPath: true },
      { name: "month", value: "2026-09", inBody: true },
      { name: "amount_cents", value: "0", inBody: true, type: "number" },
      { name: "category", value: "ai_api", inBody: true },
      { name: "title", value: "", inBody: true },
      { name: "public_note", value: "", inBody: true },
    ], confirm: "将更新指定 active 支出记录。已作废记录不可编辑。" },
  { group: "赞助", title: "作废支出", path: "/admin/sponsor/expenses/{entry_id}/void", method: "POST", danger: true,
    params: [{ name: "entry_id", value: "", inPath: true }], confirm: "将把指定支出标记为 void，公开统计不再计入，且不可删除。" },

  { group: "数据库", title: "数据库统计", path: "/database/stats" },
  { group: "数据库", title: "弹幕记录", path: "/database/danmaku",
    params: [{ name: "limit", value: "50" }, { name: "offset", value: "0" }] },
  { group: "数据库", title: "回复记录", path: "/database/replies",
    params: [{ name: "limit", value: "50" }, { name: "offset", value: "0" }] },
  { group: "数据库", title: "结构化导出", path: "/database/export",
    params: [{ name: "start_time", value: "" }, { name: "end_time", value: "" }],
    note: "导出可能很大，建议先用时间范围收窄" },

  { group: "插件", title: "插件列表", path: "/plugins" },
  { group: "插件", title: "启用插件", path: "/plugins/{plugin_name}/enable", method: "POST",
    danger: true, params: [{ name: "plugin_name", value: "", inPath: true }],
    confirm: "将启用该插件（可逆，随时可禁用）" },
  { group: "插件", title: "禁用插件", path: "/plugins/{plugin_name}/disable", method: "POST",
    danger: true, params: [{ name: "plugin_name", value: "", inPath: true }],
    confirm: "将禁用该插件（可逆，随时可启用）" },

  { group: "配置", title: "当前配置（密钥已脱敏）", path: "/config",
    note: "服务端已把 api_key / token 类字段替换为 ***" },
  { group: "配置", title: "更新配置", path: "/config", method: "PUT",
    disabled: "高危且易写坏，请用 curl 执行" }
];

// ---------------------------------------------------------------- 装配
function buildPanel(p, idx) {
  var card = el("div", { class: "card", id: "p" + idx });
  var h = el("h2", null, [
    el("span", { text: p.title }),
    el("span", { class: "path", text: (p.method || "GET") + " " + p.path }),
    el("span", { class: "caret", text: "展开" })
  ]);
  if (p.disabled) h.appendChild(el("span", { class: "tag bad", text: p.disabled }));
  else if (p.danger) h.appendChild(el("span", { class: "tag warn", text: "写操作" }));
  var body = el("div", { class: "body" });
  card.appendChild(h); card.appendChild(body);

  var built = false;
  h.onclick = function () {
    card.classList.toggle("open");
    h.querySelector(".caret").textContent = card.classList.contains("open") ? "收起" : "展开";
    if (!built && card.classList.contains("open")) { built = true; buildBody(p, body); }
  };
  return card;
}

function buildBody(p, body) {
  if (p.disabled) {
    body.appendChild(el("div", { class: "muted",
      text: "后台不提供此操作：" + p.disabled + "。示例：curl -X " + (p.method || "GET") +
            " -H 'x-admin-key: <key>' <host>" + p.path }));
    return;
  }
  if (p.custom) { p.custom(body); return; }

  var inputs = {};
  var ctl = el("div", { class: "row" });
  (p.params || []).forEach(function (q) {
    ctl.appendChild(el("span", { class: "muted", text: q.name }));
    var i = el("input", { value: q.value || "", placeholder: q.name });
    inputs[q.name] = { el: i, spec: q };
    ctl.appendChild(i);
  });
  var run = el("button", { class: p.danger ? "danger" : "primary",
    text: p.danger ? "执行（需确认）" : "刷新" });
  ctl.appendChild(run);
  body.appendChild(ctl);
  if (p.note) body.appendChild(el("div", { class: "muted", text: p.note }));
  var out = el("div", { class: "muted", text: p.danger ? "点按钮执行" : "加载中…" });
  body.appendChild(out);

  function go() {
    var path = p.path, qs = [], body = null, missing = [];
    Object.keys(inputs).forEach(function (k) {
      var v = inputs[k].el.value.trim();
      if (inputs[k].spec.inPath) {
        if (v === "") missing.push(k);
        path = path.replace("{" + k + "}", encodeURIComponent(v));
      } else if (inputs[k].spec.inBody) {
        body = body || {};
        if (inputs[k].spec.type === "number") {
          var n = Number(v);
          if (!isFinite(n)) { missing.push(k); return; }
          body[k] = n;
        } else { body[k] = v; }
      }
      else if (v !== "") qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    if (missing.length) {
      out.textContent = "";
      out.appendChild(el("div", { class: "err", text: "请先填写路径参数：" + missing.join(", ") }));
      return;
    }
    var url = path + (qs.length ? "?" + qs.join("&") : "");
    if (p.danger) {
      var text = (p.confirm || "将执行写操作") + "\\n\\n" + (p.method || "GET") + " " + url +
        "\\n\\n确认请输入 EXEC：";
      if (window.prompt(text) !== "EXEC") { setStatus("已取消写操作", ""); return; }
    }
    out.textContent = "请求中…";
    api(url, { method: p.method || "GET", body: body }).then(function (data) {
      out.textContent = "";
      out.appendChild(renderValue(data));
      setStatus("最近更新 " + new Date().toLocaleTimeString("zh-CN"), "ok");
    }).catch(function (e) {
      out.textContent = ""; out.appendChild(el("div", { class: "err", text: String(e.message || e) }));
    });
  }
  run.onclick = go;
  body.__reload = go;
  if (!p.danger) go();
}

function mount() {
  var nav = document.getElementById("nav"), host = document.getElementById("panels");
  nav.textContent = ""; host.textContent = "";
  var seen = {};
  PANELS.forEach(function (p, i) {
    if (!seen[p.group]) {
      seen[p.group] = true;
      nav.appendChild(el("div", { class: "grp", text: p.group }));
      host.appendChild(el("h3", { text: p.group, style: "font-size:13px;margin:16px 0 6px" }));
    }
    nav.appendChild(el("a", { href: "#p" + i, text: p.title }));
    host.appendChild(buildPanel(p, i));
  });
  // 首屏默认展开总览与 Token 用量：这两个是每天真正要看的东西。
  [0, 1].forEach(function (i) {
    var c = document.getElementById("p" + i);
    if (c) c.querySelector("h2").click();
  });
}

function reloadOpen() {
  document.querySelectorAll(".card.open .body").forEach(function (b) {
    if (b.__reload) b.__reload();
  });
}

document.getElementById("save").onclick = function () {
  var input = document.getElementById("key");
  if (input.value) { setKey(input.value); input.value = ""; }
  if (!getKey()) { setStatus("请先粘贴密钥", "bad"); return; }
  setStatus("连接中…", "");
  api("/admin/tokens/stats").then(function () {
    setStatus("已连接", "ok"); mount();
  }).catch(function (e) { setStatus(String(e.message || e), "bad"); });
};
document.getElementById("forget").onclick = function () {
  setKey(""); setStatus("密钥已清除", "");
  document.getElementById("nav").textContent = "";
  document.getElementById("panels").textContent = "";
};

var autoTimer = null;
function syncAuto() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  if (document.getElementById("auto").checked) {
    // 最小 30s：admin 桶经不起更密的轮询。
    var sec = Math.max(30, parseInt(document.getElementById("autoSec").value, 10) || 60);
    autoTimer = setInterval(reloadOpen, sec * 1000);
  }
}
document.getElementById("auto").onchange = syncAuto;
document.getElementById("autoSec").onchange = syncAuto;

if (getKey()) document.getElementById("save").click();
else setStatus("请粘贴 ADMIN__API_KEY 后点「连接」", "");
</script>
</body>
</html>
"""

__all__ = ["ADMIN_UI_HTML"]

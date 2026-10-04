/* ChatBI 单页逻辑（T8 / SPEC §6.4）——原生 JS + fetch，无框架、无外部依赖。
 *
 * 约束：所有服务端文本（结论 / SQL / 错误 / trace 事件）一律经 textContent 渠道
 * 渲染（el() 与 inlineMd()），不拼 innerHTML，防注入；结论是 Markdown 文本，
 * 用轻量渲染器（标题 / 表格 / 列表 / **粗体** / `代码`）转 DOM。 */
"use strict";

// ---------- 基础：DOM 构造与请求 ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

const $ = (id) => document.getElementById(id);

const fmtNum = (n) => (typeof n === "number" ? n.toLocaleString("zh-CN") : String(n ?? ""));

async function fetchJSON(url, options) {
  const resp = await fetch(url, options);
  let body = null;
  try { body = await resp.json(); } catch { /* 非 JSON 错误体，走状态码 */ }
  if (!resp.ok) {
    const detail = body && body.detail
      ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))
      : `HTTP ${resp.status}`;
    throw new Error(detail);
  }
  return body;
}

// ---------- 轻量 Markdown 渲染（标题 / 表格 / 列表 / 粗体 / 行内代码） ----------

function inlineMd(target, text) {
  for (const part of String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**")) {
      target.appendChild(el("strong", { text: part.slice(2, -2) }));
    } else if (part.startsWith("`") && part.endsWith("`")) {
      target.appendChild(el("code", { text: part.slice(1, -1) }));
    } else {
      target.appendChild(document.createTextNode(part));
    }
  }
}

function splitMdRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function renderMarkdown(container, md) {
  container.replaceChildren();
  const lines = String(md || "").split("\n");
  const para = [];
  const flush = () => {
    if (!para.length) return;
    const p = el("p");
    inlineMd(p, para.join("\n"));
    container.appendChild(p);
    para.length = 0;
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const isRow = /^\s*\|.*\|\s*$/.test(line);
    if (isRow && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      const header = splitMdRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(splitMdRow(lines[i++]));
      const thead = el("thead", {}, el("tr", {}, ...header.map((c) => el("th", { text: c }))));
      const tbody = el("tbody");
      for (const row of rows) {
        const tr = el("tr");
        header.forEach((_, j) => { const td = el("td"); inlineMd(td, row[j] ?? ""); tr.appendChild(td); });
        tbody.appendChild(tr);
      }
      container.appendChild(el("table", {}, thead, tbody));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const level = Math.min(heading[1].length + 3, 6); // 页面已有 h1~h3，结论标题降档
      const h = el(`h${level}`);
      inlineMd(h, heading[2]);
      container.appendChild(h);
      i += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flush();
      const ul = el("ul");
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        const li = el("li");
        inlineMd(li, lines[i].replace(/^\s*[-*]\s+/, ""));
        ul.appendChild(li);
        i += 1;
      }
      container.appendChild(ul);
      continue;
    }
    if (line.trim() === "") { flush(); i += 1; continue; }
    para.push(line);
    i += 1;
  }
  flush();
}

// ---------- 证据 / 步骤 / 用量（结果区与 trace 详情共用） ----------

const OUTCOME_CLASS = {
  ok: "ok", error: "error", invalid: "invalid",
  empty: "empty", duplicate: "duplicate", no_sql: "no_sql",
};

function renderEvidence(tbody, evidence) {
  tbody.replaceChildren();
  for (const e of evidence || []) {
    const preview = (e.rows_preview || []).slice(0, 2).map((r) => JSON.stringify(r)).join("\n");
    tbody.appendChild(el("tr", {},
      el("td", { text: e.id ?? "" }),
      el("td", { text: e.description ?? "" }),
      el("td", { class: "sql-cell" }, el("code", { text: e.sql ?? "" })),
      el("td", { text: fmtNum(e.row_count) }),
      el("td", { text: e.ms != null ? `${Math.round(e.ms)} ms` : "" }),
      el("td", { class: "preview-cell" }, el("code", { text: preview })),
    ));
  }
}

function renderSteps(tbody, steps) {
  tbody.replaceChildren();
  for (const s of steps || []) {
    const outcome = s.outcome ?? "";
    tbody.appendChild(el("tr", {},
      el("td", { text: s.step_id ?? "" }),
      el("td", { text: s.description ?? "" }),
      el("td", { text: s.attempt ?? "" }),
      el("td", { class: `outcome ${OUTCOME_CLASS[outcome] || ""}`, text: outcome }),
      el("td", { text: s.error ?? "" }),
    ));
  }
}

function renderUsage(node, usage) {
  if (!usage) { node.replaceChildren(); return; }
  node.replaceChildren(
    `用量：LLM ${usage.llm_calls ?? 0} 次 · 查询 ${usage.query_calls ?? 0} 次 · ` +
    `tokens ${usage.tokens_in ?? 0} 入 / ${usage.tokens_out ?? 0} 出` +
    (usage.wall_ms != null ? ` · 全程 ${(usage.wall_ms / 1000).toFixed(1)} s` : ""),
  );
}

// ---------- 提问与结果区 ----------

let askTimer = null;

async function onAnalyze() {
  const question = $("question").value.trim();
  if (!question) { $("ask-error").hidden = false; $("ask-error").textContent = "请输入问题。"; return; }
  $("ask-error").hidden = true;
  $("ask-btn").disabled = true;
  $("question").disabled = true;
  $("loading").hidden = false;
  const t0 = Date.now();
  askTimer = setInterval(() => {
    $("loading-text").textContent =
      `分析中（通常 30~60 秒）… 已等待 ${Math.round((Date.now() - t0) / 1000)} 秒`;
  }, 1000);
  try {
    const result = await fetchJSON("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    renderResult(question, result);
    loadTraces();
  } catch (err) {
    $("ask-error").hidden = false;
    $("ask-error").textContent = `请求失败：${err.message}`;
  } finally {
    clearInterval(askTimer);
    $("loading").hidden = true;
    $("ask-btn").disabled = false;
    $("question").disabled = false;
  }
}

function renderResult(question, result) {
  const section = $("result-section");
  section.hidden = false;
  const badge = $("status-badge");
  badge.textContent = result.status;
  badge.className = `badge ${result.status}`;
  $("result-question").textContent = question;
  renderMarkdown($("conclusion"), result.conclusion || "（无结论）");
  renderEvidence($("evidence-table").querySelector("tbody"), result.evidence);
  renderSteps($("steps-table").querySelector("tbody"), result.steps);
  renderUsage($("usage"), result.usage);

  const errBox = $("result-errors");
  const errs = result.errors || [];
  errBox.hidden = !errs.length;
  errBox.replaceChildren(...errs.map((e) => el("div", { text: `非致命错误：${e}` })));

  const traceBtn = $("view-trace-btn");
  const runId = result.trace_dir ? result.trace_dir.split(/[\\/]/).filter(Boolean).pop() : null;
  traceBtn.hidden = !runId;
  traceBtn.onclick = () => { showTraceDetail(runId); $("history-section").scrollIntoView({ behavior: "smooth" }); };
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- 库结构速览 ----------

async function loadSchema() {
  const body = $("schema-body");
  body.replaceChildren(el("span", { class: "dim", text: "加载中…" }));
  try {
    const data = await fetchJSON("/api/schema");
    body.replaceChildren();
    for (const t of data.tables || []) {
      const columns = el("table", { class: "grid" },
        el("thead", {}, el("tr", {}, el("th", { text: "字段" }), el("th", { text: "类型" }),
          el("th", { text: "可空" }))));
      const ct = el("tbody");
      for (const c of t.columns || []) {
        ct.appendChild(el("tr", {}, el("td", { text: c.name }), el("td", { text: c.type }),
          el("td", { text: c.nullable ? "√" : "" })));
      }
      columns.appendChild(ct);
      const enums = Object.entries(t.enum_values || {}).map(([col, vals]) =>
        el("div", { class: "chip", text: `${col}: ${vals.join(" / ")}` }));
      const detail = el("details", {},
        el("summary", { text: `${t.table} · ${fmtNum(t.row_count)} 行` }),
        el("div", { class: "table-wrap" }, columns),
        ...enums,
      );
      body.appendChild(detail);
    }
  } catch (err) {
    body.replaceChildren(el("div", { class: "error", text: `加载失败：${err.message}` }));
  }
}

// ---------- 历史 trace：列表与详情 ----------

async function loadTraces() {
  const errBox = $("trace-error");
  errBox.hidden = true;
  try {
    const data = await fetchJSON("/api/traces");
    const table = el("table", { class: "grid" },
      el("thead", {}, el("tr", {},
        el("th", { text: "run_id" }), el("th", { text: "时间" }), el("th", { text: "状态" }),
        el("th", { text: "问题" }), el("th", { text: "LLM/查询" }), el("th", { text: "tokens" }))));
    const tbody = el("tbody");
    for (const t of data.traces || []) {
      const status = t.status ?? "unknown";
      const tr = el("tr", {},
        el("td", { class: "id-cell", text: t.run_id }),
        el("td", { text: t.at ? t.at.slice(0, 19).replace("T", " ") : "" }),
        el("td", {}, el("span", { class: `badge ${status}`, text: status })),
        el("td", { class: "q-cell", text: t.question ?? "" }),
        el("td", { text: `${t.llm_calls ?? "-"}/${t.query_calls ?? "-"}` }),
        el("td", { text: `${t.tokens_in ?? "-"}/${t.tokens_out ?? "-"}` }),
      );
      tr.title = t.question ?? "";
      tr.onclick = () => showTraceDetail(t.run_id);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    const list = $("trace-list");
    list.replaceChildren(table);
    if (!(data.traces || []).length) {
      list.appendChild(el("p", { class: "dim", text: "暂无历史分析。" }));
    }
  } catch (err) {
    errBox.hidden = false;
    errBox.textContent = `加载历史失败：${err.message}`;
  }
}

function eventSummary(e) {
  const err = e.error ? ` · ${e.error}` : "";
  switch (e.type) {
    case "message":
      return `LLM 调用 · ${Math.round(e.ms ?? 0)} ms · tokens ${e.tokens_in}/${e.tokens_out}${err}`;
    case "tool_call":
      return `${e.tool}(${(e.args || []).map((a) => JSON.stringify(a)).join(", ")}) · ` +
        `${Math.round(e.ms ?? 0)} ms · ${e.ok ? "ok" : "失败"}` +
        (e.row_count != null ? ` · ${e.row_count} 行` : "") + err;
    case "sql_result":
      return `步骤 ${e.step_id} 第 ${e.attempt} 次尝试 → ${e.outcome}` +
        (e.row_count != null ? ` · ${e.row_count} 行` : "") +
        (e.ms != null ? ` · ${Math.round(e.ms)} ms` : "") + err;
    case "step":
      return e.event === "plan_created"
        ? `生成计划（${(e.steps || []).length} 步 · ${e.question_type}${e.replanned ? " · replan" : ""}）`
        : `步骤 ${e.step_id}：${e.from} → ${e.to}`;
    case "conclusion":
      return `终态 ${e.status}`;
    case "usage":
      return `LLM ${e.llm_calls} · 查询 ${e.query_calls} · tokens ${e.tokens_in}/${e.tokens_out}` +
        ` · 全程 ${((e.wall_ms || 0) / 1000).toFixed(1)} s`;
    default:
      return "";
  }
}

async function showTraceDetail(runId) {
  const detail = $("trace-detail");
  const body = $("trace-detail-body");
  body.replaceChildren(el("span", { class: "dim", text: "加载中…" }));
  $("trace-list").hidden = true;
  detail.hidden = false;
  try {
    const t = await fetchJSON(`/api/traces/${encodeURIComponent(runId)}`);
    body.replaceChildren();
    const status = t.status ?? "unknown";
    body.appendChild(el("div", { class: "result-head" },
      el("span", { class: `badge ${status}`, text: status }),
      el("span", { class: "dim", text: t.question ?? "" }),
      t.question_type ? el("span", { class: "chip", text: t.question_type }) : null,
      el("code", { text: runId }),
    ));
    if (t.conclusion) {
      body.appendChild(el("h3", { text: "结论" }));
      body.appendChild(el("div", { class: "conclusion md", id: "trace-conclusion" }));
      renderMarkdown($("trace-conclusion"), t.conclusion);
    }
    if ((t.evidence || []).length) {
      body.appendChild(el("h3", { text: "证据" }));
      const evTable = el("table", { class: "grid", id: "trace-evidence" },
        el("thead", {}, el("tr", {}, el("th", { text: "ID" }), el("th", { text: "说明" }),
          el("th", { text: "SQL" }), el("th", { text: "行数" }), el("th", { text: "耗时" }),
          el("th", { text: "结果预览" }))));
      evTable.appendChild(el("tbody"));
      body.appendChild(el("div", { class: "table-wrap" }, evTable));
      renderEvidence(evTable.querySelector("tbody"), t.evidence);
    }
    body.appendChild(el("h3", { text: "事件时间线" }));
    const evTable = el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, el("th", { text: "#" }), el("th", { text: "时刻" }),
        el("th", { text: "类型" }), el("th", { text: "节点" }), el("th", { text: "详情" }))));
    const tbody = el("tbody");
    for (const e of t.events || []) {
      const summaryCell = el("td");
      summaryCell.appendChild(document.createTextNode(eventSummary(e)));
      if (e.type === "message" && e.response) {
        summaryCell.appendChild(el("details", {},
          el("summary", { text: "模型回复" }),
          el("pre", { class: "raw", text: e.response }),
        ));
      }
      tbody.appendChild(el("tr", {},
        el("td", { text: e.seq ?? "" }),
        el("td", { text: e.at ? e.at.slice(11, 23) : "" }),
        el("td", { text: e.type ?? "" }),
        el("td", { text: e.node ?? "" }),
        summaryCell,
      ));
    }
    evTable.appendChild(tbody);
    body.appendChild(el("div", { class: "table-wrap" }, evTable));
    if (t.usage) {
      const usageNode = el("div", { class: "usage" });
      renderUsage(usageNode, t.usage);
      body.appendChild(usageNode);
    }
  } catch (err) {
    body.replaceChildren(el("div", { class: "error", text: `加载失败：${err.message}` }));
  }
}

function backToList() {
  $("trace-detail").hidden = true;
  $("trace-list").hidden = false;
}

// ---------- 初始化 ----------

async function checkHealth() {
  const dot = $("health");
  try {
    const body = await fetchJSON("/api/health");
    dot.className = `health ${body.status === "ok" ? "ok" : "bad"}`;
    dot.title = "服务正常";
  } catch (err) {
    dot.className = "health bad";
    dot.title = `服务异常：${err.message}`;
  }
}

$("ask-btn").addEventListener("click", onAnalyze);
$("question").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) onAnalyze();
});
$("load-schema-btn").addEventListener("click", loadSchema);
$("refresh-traces-btn").addEventListener("click", loadTraces);
$("back-to-list-btn").addEventListener("click", backToList);
checkHealth();
loadTraces();

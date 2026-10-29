/* ============================================================
   SRE On-Call Agent · 监控台前端逻辑
   - 通过 SSE (EventSource) 订阅 Agent 调查过程的实时事件
   - 支持多个调查并发：每个调查一张独立卡片
   - 渲染实时步骤、Markdown 报告、历史报告列表
   零依赖：原生 JS，无 CDN
   ============================================================ */

const $ = (sel) => document.querySelector(sel);

const state = {
  currentId: null,                    // 当前选中的事故
  nextRunId: 1,                       // 本地运行计数器
  runs: new Map(),                    // runId -> { es, incidentId, card, pct }
  runningByIncident: new Map(),       // incidentId -> runId (防止同一事故重复调查)
  reportOffset: 0,
  reportTotal: 0,
  reportKeyword: "",
};

const REPORT_PAGE_SIZE = 20;

/* ---------------- 认证 ---------------- */

const TOKEN_KEY = "sre_token";

function token() { return localStorage.getItem(TOKEN_KEY); }

async function authFetch(path, opts) {
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts && opts.headers), "Authorization": "Bearer " + token() },
  });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem("sre_username");
    location.href = "/login.html";
    throw new Error("unauthorized");
  }
  return res;
}

function checkAuth() {
  if (!token()) {
    location.href = "/login.html";
    return false;
  }
  $("#userName").textContent = localStorage.getItem("sre_username") || "";
  return true;
}

$("#logoutBtn").onclick = async () => {
  try { await fetch("/auth/logout", { headers: { "Authorization": "Bearer " + token() } }); } catch (_) {}
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem("sre_username");
  location.href = "/login.html";
};

/* ---------------- 工具函数 ---------------- */

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function logLine(consoleEl, cls, html) {
  const line = document.createElement("div");
  line.className = "cline " + cls;
  line.innerHTML = html;
  consoleEl.appendChild(line);
  if ($("#autoscroll").checked) {
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

/* ---------------- 迷你 Markdown 渲染器 ---------------- */

function inlineMd(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function buildTable(rows) {
  const cells = (row) => row.split("|").slice(1, -1).map((c) => inlineMd(c.trim()));
  let html = "<table><thead><tr>" + cells(rows[0]).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
  for (const row of rows.slice(2)) {
    if (!row.trim()) continue;
    html += "<tr>" + cells(row).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
  }
  return html + "</tbody></table>";
}

function renderMarkdown(md) {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let inCode = false, codeBuf = [], inTable = false, tableBuf = [], listOpen = false;

  const closeList = () => { if (listOpen) { html += "</ul>"; listOpen = false; } };

  for (const line of lines) {
    const t = line.trim();

    if (t.startsWith("```")) {
      if (inCode) { html += "<pre>" + codeBuf.join("\n") + "</pre>"; codeBuf = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(esc(line)); continue; }

    if (t.startsWith("|")) {
      closeList();
      if (!inTable) { inTable = true; tableBuf = []; }
      tableBuf.push(t);
      continue;
    }
    if (inTable) { html += buildTable(tableBuf); tableBuf = []; inTable = false; }

    if (/^#{1,6}\s/.test(t)) {
      closeList();
      const lvl = t.match(/^#+/)[0].length;
      html += `<h${lvl}>${inlineMd(t.replace(/^#{1,6}\s*/, ""))}</h${lvl}>`;
    } else if (/^[-*]\s/.test(t)) {
      if (!listOpen) { html += "<ul>"; listOpen = true; }
      html += "<li>" + inlineMd(t.replace(/^[-*]\s/, "")) + "</li>";
    } else if (/^\d+\.\s/.test(t)) {
      closeList();
      html += "<ol><li>" + inlineMd(t.replace(/^\d+\.\s/, "")) + "</li></ol>";
    } else if (t === "") {
      closeList();
    } else {
      closeList();
      html += "<p>" + inlineMd(t) + "</p>";
    }
  }
  if (inCode) html += "<pre>" + codeBuf.join("\n") + "</pre>";
  if (inTable) html += buildTable(tableBuf);
  closeList();
  return html;
}

/* ---------------- 调查卡片 ---------------- */

function createRunCard(incidentId, ref) {
  const runId = state.nextRunId++;
  const card = document.createElement("div");
  card.className = "run-card";
  card.id = "run-" + runId;
  card.innerHTML =
    `<div class="run-header">` +
    `<span class="run-title">${esc(incidentId)}</span>` +
    `<span class="run-status running">⏳ 调查中…</span>` +
    `<button class="run-minimize" title="最小化">—</button>` +
    `<button class="run-close" title="关闭">×</button>` +
    `</div>` +
    `<div class="run-progress"><div class="run-progress-bar"></div><span class="run-progress-text">0%</span></div>` +
    `<div class="run-console"></div>` +
    `<details class="run-report"><summary>📄 复盘报告</summary><div class="report-content"></div></details>`;
  card.querySelector(".run-close").onclick = () => closeRun(runId);
  card.querySelector(".run-minimize").onclick = () => card.classList.toggle("collapsed");
  state.runs.set(runId, { es: null, incidentId, card, pct: 0, ref });
  $("#runs").appendChild(card);
  $("#emptyHint").style.display = "none";
  return { runId, card };
}

function findRunByRef(ref) {
  for (const [runId, run] of state.runs) {
    if (run.ref === ref) return runId;
  }
  return null;
}

function focusCard(runId) {
  const run = state.runs.get(runId);
  if (!run) return;
  run.card.scrollIntoView({ behavior: "smooth", block: "start" });
  const report = run.card.querySelector(".run-report");
  if (report) report.open = true;
  run.card.classList.add("flash");
  setTimeout(() => run.card.classList.remove("flash"), 1600);
}

function updateProgress(runId, pct) {
  const run = state.runs.get(runId);
  if (!run) return;
  run.pct = Math.max(run.pct, pct);           // 进度只增不减
  run.card.querySelector(".run-progress-bar").style.width = run.pct + "%";
  run.card.querySelector(".run-progress-text").textContent = run.pct + "%";
}

function closeRun(runId) {
  const run = state.runs.get(runId);
  if (!run) return;
  if (run.es) { run.es.close(); }
  state.runningByIncident.delete(run.incidentId);
  state.runs.delete(runId);
  run.card.remove();
  if (state.runs.size === 0) {
    $("#emptyHint").style.display = "";
  }
}

function setRunStatus(runId, cls, text) {
  const run = state.runs.get(runId);
  if (!run) return;
  const badge = run.card.querySelector(".run-status");
  badge.className = "run-status " + cls;
  badge.textContent = text;
}

/* ---------------- SSE 事件处理 ---------------- */

function handleEvent(runId, ev) {
  const run = state.runs.get(runId);
  if (!run) return;
  const consoleEl = run.card.querySelector(".run-console");

  switch (ev.type) {
    case "start":
      updateProgress(runId, 5);
      logLine(consoleEl, "done", `⏳ 调查开始（SSE 连接已建立）`);
      break;

    case "stage": {
      const pctMap = { 1: { start: 10, end: 25 }, 2: { start: 30, end: 78 }, 3: { start: 82, end: 98 } };
      const phase = pctMap[ev.stage] || {};
      updateProgress(runId, ev.status === "start" ? phase.start : phase.end);
      logLine(consoleEl, "stage",
        `───── STAGE ${ev.stage}: ${esc(ev.name)} — ${ev.status === "start" ? "启动" : "完成"} ─────`);
      break;
    }

    case "plan":
      updateProgress(runId, 20);
      logLine(consoleEl, "plan-title", `📋 调查计划（${ev.steps.length} 步）：`);
      ev.steps.forEach((s, i) =>
        logLine(consoleEl, "plan",
          `&nbsp;&nbsp;${i + 1}. [${esc(s.tool)}] ${esc(s.query)} — ${esc(s.reason)}`));
      break;

    case "step": {
      const run = state.runs.get(runId);
      updateProgress(runId, Math.min(70, (run ? run.pct : 30) + 4));
      if (ev.thought)     logLine(consoleEl, "thought", `💭 Thought: ${esc(ev.thought)}`);
      if (ev.action)      logLine(consoleEl, "action", `🔧 Action: ${esc(ev.action)}`);
      if (ev.observation) logLine(consoleEl, "observation", `👀 Observation: ${esc(ev.observation)}`);
      break;
    }

    case "root_cause":
      updateProgress(runId, 75);
      logLine(consoleEl, "root-cause", `✅ 根因确认：${esc(ev.conclusion)}`);
      break;

    case "draft":
      updateProgress(runId, 88);
      logLine(consoleEl, "note", "✍️ 复盘报告初稿完成，进入自我评审…");
      break;

    case "critique":
      updateProgress(runId, 92);
      logLine(consoleEl, "critique", `🔍 自我评审 #${ev.revision}：质量分 <b>${esc(ev.score)}/10</b>`);
      break;

    case "revise":
      updateProgress(runId, 96);
      logLine(consoleEl, "note", `🔄 依据评审意见完成修订（第 ${esc(ev.revision)} 轮）`);
      break;

    case "report":
      updateProgress(runId, 100);
      logLine(consoleEl, "note", "📄 复盘报告已生成，展开下方「复盘报告」查看");
      run.card.querySelector(".run-report .report-content").innerHTML = renderMarkdown(ev.report);
      break;

    case "error":
      logLine(consoleEl, "error", `❌ 调查出错：${esc(ev.message)}`);
      break;

    case "done":
      updateProgress(runId, 100);
      logLine(consoleEl, "done", "🏁 调查结束");
      setRunStatus(runId, "done", "✅ 已完成");
      finishRun(runId);
      loadReports(state.reportKeyword, false);
      break;
  }
}

function finishRun(runId) {
  const run = state.runs.get(runId);
  if (!run) return;
  if (run.es) { run.es.close(); run.es = null; }
  state.runningByIncident.delete(run.incidentId);
}

/* ---------------- 调查流程 ---------------- */

async function runInvestigation() {
  if (!state.currentId) return;

  // 同一事故已在进行中的调查 → 提示并跳转到该卡片，不重复开
  if (state.runningByIncident.has(state.currentId)) {
    const existingRunId = state.runningByIncident.get(state.currentId);
    const run = state.runs.get(existingRunId);
    if (run) {
      logLine(run.card.querySelector(".run-console"), "warn",
        `⚠️ ${esc(state.currentId)} 已有调查在进行中，请等待完成或关闭后重试`);
      focusCard(existingRunId);
    }
    return;
  }

  const { runId, card } = createRunCard(state.currentId, "live-" + state.currentId);
  state.runningByIncident.set(state.currentId, runId);

  try {
    const session = await authFetch("/auth/session", { method: "POST" });
    if (!session.ok) throw new Error("无法建立 SSE 会话");
  } catch (err) {
    logLine(card.querySelector(".run-console"), "error", `❌ ${esc(String(err))}`);
    setRunStatus(runId, "error", "❌ 会话失败");
    finishRun(runId);
    return;
  }

  const es = new EventSource(
    `/incidents/investigate/stream?incident_id=${encodeURIComponent(state.currentId)}`
  );
  state.runs.get(runId).es = es;

  es.onmessage = (e) => {
    try { handleEvent(runId, JSON.parse(e.data)); } catch (_) { /* 忽略坏消息 */ }
  };
  es.onerror = () => {
    // 服务端关闭流（done 之后）会触发 onerror；已处理过 done 则忽略
    const run = state.runs.get(runId);
    if (run && run.es === es) finishRun(runId);
  };
}

/* ---------------- 数据加载 ---------------- */

async function loadIncidents() {
  const res = await authFetch("/incidents/fixtures");
  const data = await res.json();
  const box = $("#incidentList");
  box.innerHTML = "";
  data.incidents.forEach((id) => {
    const label = document.createElement("label");
    label.className = "incident-option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "incident";
    radio.value = id;
    radio.onchange = () => { state.currentId = id; $("#runBtn").disabled = false; };
    label.appendChild(radio);
    label.appendChild(document.createTextNode(id));
    box.appendChild(label);
  });
}

async function loadReports(keyword = "", append = false) {
  const normalizedKeyword = keyword.trim();
  if (!append) {
    state.reportOffset = 0;
    state.reportKeyword = normalizedKeyword;
  }
  const params = new URLSearchParams({
    limit: String(REPORT_PAGE_SIZE),
    offset: String(state.reportOffset),
  });
  if (state.reportKeyword) params.set("keyword", state.reportKeyword);
  const res = await authFetch("/incidents/reports?" + params.toString());
  if (!res.ok) return;
  const data = await res.json();
  const ul = $("#reportList");
  if (!append) ul.innerHTML = "";
  if (data.reports.length === 0) {
    if (!append) {
      const li = document.createElement("li");
      li.className = "report-empty";
      li.textContent = state.reportKeyword ? "没有匹配的历史报告" : "暂无历史报告";
      ul.appendChild(li);
    }
    $("#loadMoreReports").style.display = "none";
    return;
  }
  data.reports.forEach((r) => {
    const li = document.createElement("li");
    li.className = "report-item";
    const ownerLabel = r.owner_username === "system"
      ? '<span class="report-owner">系统</span> '
      : "";
    li.innerHTML =
      `<span class="sev ${esc(r.severity)}">${esc(r.severity)}</span> ` +
      `${ownerLabel}${esc(r.incident_id)} · ${esc(r.service)}` +
      `<small>${esc(r.created_at)}</small>` +
      (r.owner_username === "system"
        ? ""
        : `<button class="report-del" title="删除该报告">🗑</button>`);
    li.onclick = () => loadReport(r.id);
    const deleteButton = li.querySelector(".report-del");
    if (deleteButton) {
      deleteButton.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`确定删除报告「${r.incident_id} · ${r.created_at}」？删除后不可恢复。`)) {
          return;
        }
        const del = await authFetch(`/incidents/reports/${r.id}`, { method: "DELETE" });
        if (!del.ok) return;
        const openRunId = findRunByRef("hist-" + r.id);
        if (openRunId !== null) closeRun(openRunId);
        loadReports(state.reportKeyword, false);
      };
    }
    ul.appendChild(li);
  });
  state.reportOffset += data.reports.length;
  state.reportTotal = data.pagination.total;
  const loadMore = $("#loadMoreReports");
  loadMore.style.display = data.pagination.has_more ? "" : "none";
  loadMore.textContent = `加载更多（${state.reportOffset}/${state.reportTotal}）`;
}

/* 搜索：输入防抖 300ms 后按关键词重新加载（也可点「搜索」按钮立即触发） */
let searchTimer = null;
$("#reportSearch").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    loadReports($("#reportSearch").value.trim(), false);
  }, 300);
};
$("#searchBtn").onclick = () => {
  clearTimeout(searchTimer);
  loadReports($("#reportSearch").value.trim(), false);
};

$("#loadMoreReports").onclick = () => {
  loadReports(state.reportKeyword, true);
};

/* 清空当前用户的历史报告（永久删除，二次确认） */
$("#clearReportsBtn").onclick = async () => {
  if (!confirm("确定清空当前账号的历史报告？此操作不可恢复！")) return;
  const res = await authFetch("/incidents/reports", { method: "DELETE" });
  if (!res.ok) return;
  // 关闭所有已打开的历史报告卡片
  [...state.runs.keys()].forEach((runId) => {
    const run = state.runs.get(runId);
    if (run && run.ref && run.ref.startsWith("hist-")) closeRun(runId);
  });
  loadReports(state.reportKeyword, false);
};

async function loadReport(id) {
  // 同一历史报告已打开 → 直接跳转过去，不重复开卡片
  const existingRunId = findRunByRef("hist-" + id);
  if (existingRunId !== null) {
    focusCard(existingRunId);
    return;
  }

  const res = await authFetch(`/incidents/reports/${id}`);
  if (!res.ok) return;
  const data = await res.json();
  const { runId, card } = createRunCard(data.payload.incident_id, "hist-" + id);
  card.querySelector(".run-header .run-title").textContent =
    `${data.payload.incident_id}（历史 #${id}）`;
  setRunStatus(runId, "done", "📄 历史报告");
  card.querySelector(".run-console").style.display = "none";
  card.querySelector(".run-report .report-content").innerHTML = renderMarkdown(data.payload.report);
  card.querySelector(".run-report").open = true;
}

/* ---------------- 健康探测 ---------------- */

async function checkLLM() {
  const btn = $("#llmCheckBtn");
  const st = $("#llmStatus");
  btn.disabled = true;
  st.className = "status llm checking";
  st.textContent = "● 检测中…";
  st.title = "正在向模型发送一条最小请求，最长等待 45 秒";
  try {
    const res = await authFetch("/llm/check");
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      const shortModel = String(data.model || "unknown").split("/").pop();
      st.className = "status ok";
      st.textContent = "● 模型已连接 · " + shortModel + " · " +
        (data.latency_ms / 1000).toFixed(2) + "s";
      st.title = "模型: " + data.model + "\n接口地址: " + data.base_url +
        "\n返回内容: " + data.reply + "\n耗时: " + data.latency_ms + "ms";
    } else {
      st.className = "status bad";
      st.textContent = "● " + (data.message || "模型连接失败");
      st.title = [data.hint, data.error || data.detail].filter(Boolean).join("\n");
      console.error("LLM check failed:", data);
    }
  } catch (e) {
    st.className = "status bad";
    st.textContent = "● 检测请求失败";
    st.title = String(e);
  } finally {
    btn.disabled = false;
  }
}

async function pollHealth() {
  try {
    const res = await fetch("/health");
    const ok = res.ok && (await res.json()).status === "ok";
    $("#status").className = "status " + (ok ? "ok" : "bad");
    $("#status").textContent = ok ? "● 服务正常" : "● 服务异常";
  } catch (_) {
    $("#status").className = "status bad";
    $("#status").textContent = "● 服务离线";
  }
}

/* ---------------- 初始化 ---------------- */

if (!checkAuth()) throw new Error("not authenticated");

$("#runBtn").onclick = runInvestigation;
$("#llmCheckBtn").onclick = checkLLM;

loadIncidents();
loadReports(state.reportKeyword, false);
pollHealth();
setInterval(pollHealth, 15000);

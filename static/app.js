/* ============================================================
   SRE On-Call Agent · 监控台前端逻辑
   - 通过 SSE (EventSource) 订阅 Agent 调查过程的实时事件
   - 渲染实时步骤、Markdown 报告、历史报告列表
   零依赖：原生 JS，无 CDN
   ============================================================ */

const $ = (sel) => document.querySelector(sel);

const state = { es: null, currentId: null };

/* ---------------- 工具函数 ---------------- */

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function logLine(cls, html) {
  const line = document.createElement("div");
  line.className = "cline " + cls;
  line.innerHTML = html;
  $("#console").appendChild(line);
  if ($("#autoscroll").checked) {
    $("#console").scrollTop = $("#console").scrollHeight;
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
  for (const row of rows.slice(2)) {          // 跳过 |---|---| 分隔行
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

/* ---------------- SSE 事件处理 ---------------- */

function handleEvent(ev) {
  switch (ev.type) {
    case "start":
      logLine("done", `⏳ 开始调查事故 <b>${esc(ev.incident_id)}</b>（连接已建立）`);
      break;

    case "stage":
      logLine("stage",
        `───── STAGE ${ev.stage}: ${esc(ev.name)} — ${ev.status === "start" ? "启动" : "完成"} ─────`);
      break;

    case "plan":
      logLine("plan-title", `📋 调查计划（${ev.steps.length} 步）：`);
      ev.steps.forEach((s, i) =>
        logLine("plan",
          `&nbsp;&nbsp;${i + 1}. [${esc(s.tool)}] ${esc(s.query)} — ${esc(s.reason)}`));
      break;

    case "step":
      if (ev.thought)     logLine("thought", `💭 Thought: ${esc(ev.thought)}`);
      if (ev.action)      logLine("action", `🔧 Action: ${esc(ev.action)}`);
      if (ev.observation) logLine("observation", `👀 Observation: ${esc(ev.observation)}`);
      break;

    case "root_cause":
      logLine("root-cause", `✅ 根因确认：${esc(ev.conclusion)}`);
      break;

    case "draft":
      logLine("note", "✍️ 复盘报告初稿完成，进入自我评审…");
      break;

    case "critique":
      logLine("critique", `🔍 自我评审 #${ev.revision}：质量分 <b>${esc(ev.score)}/10</b>`);
      break;

    case "revise":
      logLine("note", `🔄 依据评审意见完成修订（第 ${esc(ev.revision)} 轮）`);
      break;

    case "report":
      logLine("note", "📄 复盘报告已生成，点击上方「复盘报告」标签查看");
      showReport(ev.report);
      break;

    case "error":
      logLine("error", `❌ 调查出错：${esc(ev.message)}`);
      break;

    case "done":
      logLine("done", "🏁 调查结束");
      finishStream();
      loadReports();
      break;
  }
}

function finishStream() {
  if (state.es) { state.es.close(); state.es = null; }
  $("#runBtn").disabled = false;
}

/* ---------------- 调查流程 ---------------- */

function runInvestigation() {
  if (!state.currentId) return;
  $("#console").innerHTML = "";
  $("#reportContent").innerHTML = '<div class="placeholder">调查完成后自动生成复盘报告</div>';
  $("#runBtn").disabled = true;

  if (state.es) state.es.close();

  const es = new EventSource(
    `/incidents/investigate/stream?incident_id=${encodeURIComponent(state.currentId)}`
  );
  state.es = es;

  es.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (_) { /* 忽略坏消息 */ }
  };
  es.onerror = () => {
    // 服务端关闭流（done 之后）会触发 onerror；已处理过 done 则忽略
    if (state.es === es) finishStream();
  };
}

/* ---------------- 数据加载 ---------------- */

async function loadIncidents() {
  const res = await fetch("/incidents/fixtures");
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

async function loadReports() {
  const res = await fetch("/incidents/reports");
  if (!res.ok) return;
  const data = await res.json();
  const ul = $("#reportList");
  ul.innerHTML = "";
  data.reports.forEach((r) => {
    const li = document.createElement("li");
    li.className = "report-item";
    li.innerHTML =
      `<span class="sev ${esc(r.severity)}">${esc(r.severity)}</span> ` +
      `${esc(r.incident_id)} · ${esc(r.service)}` +
      `<small>${esc(r.created_at)}</small>`;
    li.onclick = () => loadReport(r.id);
    ul.appendChild(li);
  });
}

async function loadReport(id) {
  const res = await fetch(`/incidents/reports/${id}`);
  if (!res.ok) return;
  const data = await res.json();
  switchTab("report");
  $("#reportContent").innerHTML = renderMarkdown(data.payload.report);
}

/* ---------------- 状态与标签页 ---------------- */

function switchTab(tab) {
  document.querySelectorAll(".tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) =>
    p.classList.toggle("active", p.id === tab));
}

function showReport(md) {
  $("#reportContent").innerHTML = renderMarkdown(md);
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

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.onclick = () => switchTab(btn.dataset.tab);
});
$("#runBtn").onclick = runInvestigation;

loadIncidents();
loadReports();
pollHealth();
setInterval(pollHealth, 15000);
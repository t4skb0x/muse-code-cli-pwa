"use strict";
const $ = s => document.querySelector(s);
let curDir = "", curFile = "";
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  return r.json();
}
function showLogin() { $("#login").hidden = false; $("#app").hidden = true; }
function showApp() { $("#login").hidden = true; $("#app").hidden = false; loadChat(); loadFiles(""); }
$("#loginBtn").onclick = async () => {
  const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: $("#tok").value }) });
  if (r.ok) showApp(); else $("#loginErr").textContent = "Bad token — try again.";
};
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showLogin(); };
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("on")); b.classList.add("on");
  ["chat", "tasks", "files", "term", "diff"].forEach(t => $("#tab-" + t).hidden = t !== b.dataset.tab);
  if (b.dataset.tab === "tasks") { loadGoals(); loadTasks(); loadMonitors(); }
});
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
async function loadChat() {
  try { const d = await api("/api/chat"); $("#msgs").innerHTML = d.messages.map(m => `<div class="msg ${m.from === "agent" ? "agent" : ""}"><small>${esc(m.from)} · ${esc(m.ts)}</small>${esc(m.text)}</div>`).join("") || "<p>No messages yet.</p>"; } catch (e) {}
}
$("#chatForm").onsubmit = async e => { e.preventDefault(); const t = $("#chatIn").value.trim(); if (!t) return; $("#chatIn").value = ""; await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) }); loadChat(); };
setInterval(() => { if (!$("#tab-chat").hidden && !$("#app").hidden) loadChat(); }, 3000);
async function loadFiles(dir) {
  curDir = dir;
  const d = await api("/api/files?path=" + encodeURIComponent(dir));
  $("#cwdLabel").textContent = "" + (d.path === "." ? "" : d.path);
  const up = dir ? `<li class="dir" data-up="1">.. up</li>` : "";
  $("#fileList").innerHTML = up + d.items.map(i => `<li class="${i.dir ? "dir" : ""}" data-name="${esc(i.name)}" data-dir="${i.dir}">${esc(i.name)}${i.dir ? "/" : " · " + i.size + "B"}</li>`).join("");
  document.querySelectorAll("#fileList li").forEach(li => li.onclick = () => {
    if (li.dataset.up) return loadFiles(dir.split("/").slice(0, -1).join("/"));
    const p = dir ? dir + "/" + li.dataset.name : li.dataset.name;
    if (li.dataset.dir === "true") loadFiles(p); else openFile(p);
  });
}
async function openFile(p) {
  const d = await api("/api/file?path=" + encodeURIComponent(p));
  if (d.error) return alert(d.error);
  curFile = p; $("#vName").textContent = p; $("#vBody").value = d.content; $("#viewer").hidden = false;
}
$("#vClose").onclick = () => $("#viewer").hidden = true;
$("#vSave").onclick = async () => { const d = await api("/api/file/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: curFile, content: $("#vBody").value }) }); alert(d.ok ? "Saved (previous copy in data/backups/)" : d.error); };
$("#termForm").onsubmit = async e => {
  e.preventDefault(); const cmd = $("#termIn").value; $("#termIn").value = "";
  $("#termOut").textContent += "\n$ " + cmd + "\n… running (30s cap)";
  const d = await api("/api/exec", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cmd, cwd: curDir }) });
  $("#termOut").textContent += "\n" + (d.output || "") + "\n[exit " + d.code + "]";
  $("#termOut").scrollTop = 1e9;
};
$("#diffBtn").onclick = async () => { const d = await api("/api/git/diff?path=" + encodeURIComponent(curDir)); $("#diffOut").textContent = d.output || "(clean)"; };
async function loadGoals() {
  const d = await api("/api/goals");
  $("#goalList").innerHTML = d.goals.map(g => `<div class="msg"><small>GOAL · ${esc(g.status)} · ${g.percent}%</small>${esc(g.objective)}<div style="background:#0b0e11;height:8px"><div style="background:var(--acc);height:8px;width:${g.percent}%"></div></div>${g.status === "active" ? `<button data-gc="${g.id}">Complete</button> <button data-gb="${g.id}">Blocked</button>` : ""}</div>`).join("") || "<p>No goals yet.</p>";
  $("#taskGoal").innerHTML = '<option value="">(none)</option>' + d.goals.filter(g => g.status === "active").map(g => `<option value="${g.id}">${esc(g.objective.slice(0, 40))}</option>`).join("");
  document.querySelectorAll("[data-gc]").forEach(b => b.onclick = () => goalUpdate(b.dataset.gc, "complete", 100));
  document.querySelectorAll("[data-gb]").forEach(b => b.onclick = () => goalUpdate(b.dataset.gb, "blocked", null));
}
async function goalUpdate(id, status, percent) { await api("/api/goals/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, status, ...(percent !== null ? { percent } : {}) }) }); loadGoals(); }
$("#goalForm").onsubmit = async e => { e.preventDefault(); await api("/api/goals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ objective: $("#goalIn").value }) }); $("#goalIn").value = ""; loadGoals(); };
$("#taskBtn").onclick = async () => {
  const model = $("#taskModel").value.trim();
  await api("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("#taskIn").value, repo: $("#taskRepo").value, model, effort: $("#taskEffort").value, autonomy: $("#taskAuto").value, priority: $("#taskPriority").value, goal_id: $("#taskGoal").value }) });
  $("#taskIn").value = ""; taskPage = 1; loadTasks();
};
let taskFilter = "", taskPage = 1, taskPages = 1;
const TASK_PER_PAGE = 15;
$("#taskFilter").onchange = e => { taskFilter = e.target.value; taskPage = 1; loadTasks(); };
$("#taskPrev").onclick = () => { if (taskPage > 1) { taskPage--; loadTasks(); } };
$("#taskNext").onclick = () => { if (taskPage < taskPages) { taskPage++; loadTasks(); } };
function fmtDur(s) {
  s = Math.max(0, Math.floor(s || 0));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h) return h + "h " + (m % 60) + "m";
  return m + "m " + (s % 60) + "s";
}
function fmtAgo(ts, now) {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  return fmtDur(s) + " ago";
}
let expandedIds = new Set(); // result previews kept open across refreshes
function taskHtml(t, i) {
  const nowS = Math.floor(Date.now() / 1000);
  const elapsed = t.elapsed_seconds || 0;
  const live = t.status === "running" ? ` <span data-live-elapsed="${elapsed}" data-live-at="${nowS}">elapsed ${fmtDur(elapsed)}</span>` : `elapsed ${fmtDur(elapsed)}`;
  const timeout = t.status === "running" && t.timeout_at ? ` · times out in ${fmtDur(t.timeout_at - nowS)}` : "";
  const worker = t.worker ? ` · @${esc(t.worker)}` : "";
  const prog = (t.progress && t.progress.length) ? `<div class="prog">▸ ${esc(t.progress[t.progress.length - 1].message)}</div>` : "";
  const xp = expandedIds.has(t.id);
  const prev = t.result ? `<div>${esc(t.result.slice(0, 300))}${t.result.length > 300 ? "…" : ""} <button data-tx="${i}" data-tid="${esc(t.id)}">${xp ? "collapse" : "expand"}</button></div><pre id="tres-${i}"${xp ? "" : " hidden"}>${esc(t.result)}</pre>` : "";
  return `<div class="msg"><small>${esc(t.status)} · ${esc(t.priority || "normal")} · ${esc(t.model)} · ${esc(t.effort)} · ${esc(t.autonomy)} · ${esc(t.repo)}${worker}</small><br><small>${live}${timeout}</small><br>${esc(t.text)}${prog}${t.diff ? `<pre>${esc(t.diff)}</pre>` : ""}${prev}${t.status === "awaiting_approval" ? ` <button data-ta="${t.id}">Approve &amp; apply</button> <button data-tr="${t.id}">Reject</button>` : ""}${t.status === "failed" ? ` <button data-retry="${t.id}">Retry</button>` : ""}</div>`;
}
let lastTasks = [];
async function loadTasks() {
  const qs = `?page=${taskPage}&per_page=${TASK_PER_PAGE}${taskFilter ? "&status=" + encodeURIComponent(taskFilter) : ""}`;
  const d = await api("/api/tasks" + qs);
  lastTasks = d.tasks;
  taskPages = Math.max(1, Math.ceil((d.total || 0) / (d.per_page || TASK_PER_PAGE)));
  $("#taskList").innerHTML = d.tasks.map(taskHtml).join("") || "<p>No tasks yet.</p>";
  $("#taskPageInfo").textContent = `page ${d.page}/${taskPages} · ${d.total} total`;
  document.querySelectorAll("[data-ta]").forEach(b => b.onclick = () => taskVerdict(b.dataset.ta, "approve"));
  document.querySelectorAll("[data-tr]").forEach(b => b.onclick = () => taskVerdict(b.dataset.tr, "reject"));
  document.querySelectorAll("[data-retry]").forEach(b => b.onclick = () => taskRetry(b.dataset.retry));
  document.querySelectorAll("[data-tx]").forEach(b => b.onclick = () => { const p = $("#tres-" + b.dataset.tx); p.hidden = !p.hidden; b.textContent = p.hidden ? "expand" : "collapse"; if (p.hidden) expandedIds.delete(b.dataset.tid); else expandedIds.add(b.dataset.tid); });
  loadOrchestrator();
  loadWorkers();
}
async function taskVerdict(id, v) { await api("/api/tasks/" + v, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); loadTasks(); }
async function taskRetry(id) { const d = await api("/api/tasks/retry", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); if (d.error) alert(d.error); loadTasks(); }
function fmtShortAgo(ts, now) {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  return Math.floor(m / 60) + "h ago";
}
async function loadOrchestrator() {
  try {
    const d = await api("/api/orchestrator/status");
    const dead = d.status === "dead";
    const ago = d.last_heartbeat ? fmtShortAgo(d.last_heartbeat, d.now) : "never";
    $("#orchPanel").innerHTML = `<div class="msg"${dead ? ' style="border-color:var(--bad)"' : ""}><small>SPARK ORCHESTRATOR · ${dead ? "DEAD" : "ALIVE"}</small>Spark last update: ${esc(ago)}${dead ? ' · <strong style="color:var(--bad)">STALE — no heartbeat for over 6 minutes</strong>' : ""}</div>`;
  } catch (e) {}
}
async function loadWorkers() {
  try {
    const d = await api("/api/workers");
    $("#workerPanel").innerHTML = d.workers.map(w => {
      const proc = w.pid ? (w.alive === true ? ` · pid ${w.pid} alive` : w.alive === false ? ` · pid ${w.pid} DEAD` : ` · pid ${w.pid}`) : "";
      const dead = w.death_reason ? ` · DIED` : "";
      return `<div class="msg"><small>@${esc(w.worker)} · heartbeat ${fmtAgo(w.last_heartbeat, d.now)}${proc}${dead}</small>${w.current_task ? `running: ${esc(w.current_task_text)}` : "idle"} <button data-wd="${esc(w.worker)}">detail</button></div>`;
    }).join("") || "<p>No workers seen yet.</p>";
    document.querySelectorAll("[data-wd]").forEach(b => b.onclick = () => loadWorkerDetail(b.dataset.wd));
  } catch (e) {}
}
let workerDetailId = null;
async function loadWorkerDetail(id) {
  workerDetailId = id;
  const st = await api("/api/workers/" + encodeURIComponent(id) + "/status");
  if (st.error) { $("#workerDetail").innerHTML = `<p>${esc(st.error)}</p>`; return; }
  const procLine = st.pid
    ? (st.alive === true ? `pid ${st.pid} — ALIVE` : st.alive === false ? `pid ${st.pid} — NOT RUNNING` : `pid ${st.pid} — unknown host`)
    : "pid unknown (worker claimed without process tracking)";
  const hbLine = st.last_heartbeat
    ? `last heartbeat ${fmtAgo(st.last_heartbeat, st.now)}${st.heartbeat_stale ? " (STALE)" : ""}`
    : "no heartbeat yet";
  let logHtml = "";
  if (st.current_task) {
    try {
      const log = await api("/api/tasks/" + encodeURIComponent(st.current_task) + "/log?lines=30");
      logHtml = `<h4>Recent output — task ${esc(st.current_task)}</h4><pre>${esc((log.lines || []).join("\n")) || "(no output captured)"}</pre>`;
    } catch (e) { logHtml = "<p>Log unavailable.</p>"; }
  }
  $("#workerDetail").innerHTML = `<div class="msg"><small>WORKER DETAIL · @${esc(st.worker)}</small><br>`
    + `<small>process: ${esc(procLine)}</small><br>`
    + (st.cmd ? `<small>cmd: ${esc(st.cmd)}</small><br>` : "")
    + `<small>${esc(hbLine)}</small><br>`
    + `<small>current task: ${st.current_task ? esc(st.current_task) + " — " + esc(st.current_task_text) : "none"}</small>`
    + (st.death_reason ? `<br><small>DEATH: ${esc(st.death_reason)}</small>` : "")
    + `<br><button id="wdClose">Close</button></div>` + logHtml;
  $("#wdClose").onclick = () => { $("#workerDetail").innerHTML = ""; workerDetailId = null; };
}
function fmtWhen(ts, now) {
  if (!ts) return "never";
  return fmtAgo(ts, now) + " (" + new Date(ts * 1000).toLocaleTimeString() + ")";
}
function fmtNext(ts) {
  if (!ts) return "—";
  const s = Math.max(0, ts - Math.floor(Date.now() / 1000));
  return "in " + fmtDur(s) + " (" + new Date(ts * 1000).toLocaleTimeString() + ")";
}
async function loadMonitors() {
  try {
    const d = await api("/api/monitors");
    $("#monList").innerHTML = d.monitors.map(m => `<div class="msg"><small>MONITOR · ${esc(m.type)} · every ${m.interval}s · active</small>${esc(m.name)}${m.target ? ` · target ${esc(m.target)}` : " · all tasks"}<br><small>last check: ${fmtWhen(m.last_check, Math.floor(Date.now() / 1000))} · next: ${fmtNext(m.next_check)} · checks: ${m.check_count || 0}</small>${m.last_result ? `<div class="prog">▸ ${esc(m.last_result)}</div>` : ""} <button data-mdel="${esc(m.name)}">Remove</button></div>`).join("") || "<p>No monitors yet — they stay on until you remove them.</p>";
    document.querySelectorAll("[data-mdel]").forEach(b => b.onclick = async () => { if (!confirm("Remove monitor " + b.dataset.mdel + "? It will stop checking.")) return; await fetch("/api/monitors/" + encodeURIComponent(b.dataset.mdel), { method: "DELETE" }); loadMonitors(); });
  } catch (e) {}
}
$("#monForm").onsubmit = async e => {
  e.preventDefault();
  const d = await api("/api/monitors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#monName").value, type: $("#monType").value, interval: +$("#monInterval").value || 60, target: $("#monTarget").value }) });
  if (d.error) return alert(d.error);
  $("#monName").value = ""; $("#monTarget").value = ""; loadMonitors();
};
// Tick live running durations every second without a server round-trip.
setInterval(() => {
  const nowS = Math.floor(Date.now() / 1000);
  document.querySelectorAll("[data-live-elapsed]").forEach(el => {
    const s = (+el.dataset.liveElapsed) + (nowS - (+el.dataset.liveAt));
    el.textContent = "elapsed " + fmtDur(s);
  });
}, 1000);
setInterval(() => { if (!$("#tab-tasks").hidden && !$("#app").hidden) { loadTasks(); loadGoals(); loadMonitors(); loadOrchestrator(); } }, 5000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
api("/api/me").then(showApp).catch(showLogin);
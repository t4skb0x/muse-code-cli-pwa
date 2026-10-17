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
  if (b.dataset.tab === "tasks") { loadGoals(); loadTasks(); }
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
  loadWorkers();
}
async function taskVerdict(id, v) { await api("/api/tasks/" + v, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); loadTasks(); }
async function taskRetry(id) { const d = await api("/api/tasks/retry", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); if (d.error) alert(d.error); loadTasks(); }
async function loadWorkers() {
  try {
    const d = await api("/api/workers");
    $("#workerPanel").innerHTML = d.workers.map(w => `<div class="msg"><small>@${esc(w.worker)} · heartbeat ${fmtAgo(w.last_heartbeat, d.now)}</small>${w.current_task ? `running: ${esc(w.current_task_text)}` : "idle"}</div>`).join("") || "<p>No workers seen yet.</p>";
  } catch (e) {}
}
// Tick live running durations every second without a server round-trip.
setInterval(() => {
  const nowS = Math.floor(Date.now() / 1000);
  document.querySelectorAll("[data-live-elapsed]").forEach(el => {
    const s = (+el.dataset.liveElapsed) + (nowS - (+el.dataset.liveAt));
    el.textContent = "elapsed " + fmtDur(s);
  });
}, 1000);
setInterval(() => { if (!$("#tab-tasks").hidden && !$("#app").hidden) { loadTasks(); loadGoals(); } }, 5000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
api("/api/me").then(showApp).catch(showLogin);
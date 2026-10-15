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
  const model = $("#taskModelFree").value.trim() || $("#taskModel").value;
  await api("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("#taskIn").value, repo: $("#taskRepo").value, model, effort: $("#taskEffort").value, autonomy: $("#taskAuto").value, goal_id: $("#taskGoal").value }) });
  $("#taskIn").value = ""; loadTasks();
};
async function loadTasks() {
  const d = await api("/api/tasks");
  $("#taskList").innerHTML = d.tasks.map(t => `<div class="msg"><small>${esc(t.status)} · ${esc(t.model)} · ${esc(t.effort)} · ${esc(t.autonomy)} · ${esc(t.repo)}</small>${esc(t.text)}${t.diff ? `<pre>${esc(t.diff)}</pre>` : ""}${t.result ? `<pre>${esc(t.result.slice(-1500))}</pre>` : ""}${t.status === "awaiting_approval" ? `<button data-ta="${t.id}">Approve &amp; apply</button> <button data-tr="${t.id}">Reject</button>` : ""}</div>`).join("") || "<p>No tasks yet.</p>";
  document.querySelectorAll("[data-ta]").forEach(b => b.onclick = () => taskVerdict(b.dataset.ta, "approve"));
  document.querySelectorAll("[data-tr]").forEach(b => b.onclick = () => taskVerdict(b.dataset.tr, "reject"));
}
async function taskVerdict(id, v) { await api("/api/tasks/" + v, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); loadTasks(); }
setInterval(() => { if (!$("#tab-tasks").hidden && !$("#app").hidden) { loadTasks(); loadGoals(); } }, 5000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
api("/api/me").then(showApp).catch(showLogin);
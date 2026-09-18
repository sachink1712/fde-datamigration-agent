const API = window.MIGRATION_API_URL || "http://localhost:8000";
let run;
const $ = (id) => document.getElementById(id);
const api = async (path, options = {}) => { const response = await fetch(`${API}${path}`, {headers:{"Content-Type":"application/json"}, ...options}); if(!response.ok) throw new Error(await response.text()); return response.json(); };
function render() {
  if (!run) return;
  $("summary").innerHTML = `<strong>${run.status.replaceAll("_", " ")}</strong><p>${run.records.length} reconciled records · ${run.escalations.filter(x=>x.status==="open").length} open reviews</p>`;
  $("events").innerHTML = run.events.map(e=>`<li><b>${e.stage}</b> ${e.message}</li>`).join("");
  $("reviews").innerHTML = run.escalations.map(e => `<article class="review"><b>${e.type.replaceAll("_", " ")}</b><p>${e.reason}</p><code>${e.field}</code><p>${e.status}</p>${e.status === "open" ? `<button data-id="${e.id}" data-action="approved">Approve</button> <button data-id="${e.id}" data-action="rejected">Reject</button>` : ""}</article>`).join("") || "Nothing awaiting review.";
  $("outcomes").innerHTML = (run.outcomes || []).map(o=>`<p><b>${o.employee_id || "record"}</b>: ${o.status}${o.reason ? ` — ${o.reason}` : ""}</p>`).join("") || "No target calls yet.";
  $("push").disabled = run.status !== "ready_to_push";
  $("retry").disabled = run.status !== "push_failed";
  $("rollback").disabled = !["completed", "push_failed"].includes(run.status);
  document.querySelectorAll("[data-id]").forEach(btn => btn.onclick = async () => { const escalation = run.escalations.find(x=>x.id===btn.dataset.id); const selected_value = escalation.type === "ambiguous_mapping" ? "start_date" : null; run = await api(`/api/runs/${run.id}/escalations/${btn.dataset.id}`, {method:"POST",body:JSON.stringify({action:btn.dataset.action,selected_value})}); render(); });
}
$("start").onclick = async () => { run = await api("/api/runs", {method:"POST"}); $("notice").textContent = "Migration paused only for genuine review cases."; render(); };
$("push").onclick = async () => { run = await api(`/api/runs/${run.id}/push`, {method:"POST"}); render(); };
$("retry").onclick = async () => { run = await api(`/api/runs/${run.id}/push?retry=true`, {method:"POST"}); render(); };
$("rollback").onclick = async () => { run = await api(`/api/runs/${run.id}/rollback`, {method:"POST"}); render(); };

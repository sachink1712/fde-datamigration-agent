const API = window.MIGRATION_API_URL || "http://localhost:8000";
let run;
const $ = (id) => document.getElementById(id);
const esc = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

async function api(path, options = {}) {
  let response;
  try { response = await fetch(API + path, options); }
  catch { throw Error("Cannot reach backend at " + API + ". Start FastAPI, then refresh this page."); }
  if (!response.ok) throw Error((await response.json().catch(() => ({ detail: response.statusText }))).detail || response.statusText);
  return response.json();
}

function showFiles() {
  $("file-list").innerHTML = [...$("files").files].map((file) => `<span class="file-chip">${esc(file.name)} · ${Math.ceil(file.size / 1024)} KB</span>`).join("");
}

function preview(sample) {
  const identifier = sample.employee_id || sample.record || "record";
  const before = sample.source_value ?? sample.value ?? "";
  const after = sample.cleaned_value;
  return `<span><b>${esc(identifier)}</b> → ${esc(before)}${after !== undefined ? ` → ${esc(after)}` : ""}</span>`;
}

const blank = (v) => (v === "" || v === null || v === undefined ? "<i>(blank)</i>" : esc(v));

function fieldsTable(item) {
  const rows = item.context?.fields || [];
  if (!rows.length) return "";
  return `<table class="review-table"><thead><tr><th>Field</th><th>Source value</th><th>Cleaned value</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.field)}</td><td>${blank(r.source)}</td><td>${blank(r.cleaned)}</td></tr>`).join("")}</tbody></table>`;
}

function comparisonTable(item) {
  const cmp = item.context?.comparison;
  if (!cmp) return "";
  return `<table class="review-table compare"><thead><tr><th>Field</th>${cmp.records.map((id) => `<th>${esc(id)}</th>`).join("")}</tr></thead><tbody>${cmp.fields.map((f) => `<tr class="${f.differs ? "differs" : ""}"><td>${esc(f.field)}</td>${cmp.records.map((id) => `<td>${blank(f.values[id])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function recordCard(item) {
  const cmp = comparisonTable(item);
  const suggestion = item.suggested_fix ? `<p class="suggestion"><b>Suggested fix:</b> ${esc(item.suggested_fix.label)}</p>` : "";
  const fieldPick = (item.fields || []).length > 1 ? `<select class="correct-field">${item.fields.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("")}</select>` : "";
  const correction = item.actions.some((a) => a.action === "corrected") && item.status === "open" ? `<div class="correction">${fieldPick}<input class="correct-value" placeholder="Corrected value"></div>` : "";
  const candidates = (item.candidates || []).map((v, i) => `<label class="target-option"><input type="radio" name="mapping-${esc(item.id)}" value="${esc(v)}"><span>${esc(v)}</span></label>`).join("");
  const buttons = item.actions.map((a) => `<button data-id="${esc(item.id)}" data-action="${esc(a.action)}" class="${a.default ? "default-action" : ""}">${esc(a.label)}</button>`).join("");
  return `<article class="review"><div class="review-header"><b>${esc(item.type.replaceAll("_", " "))}</b><span class="badge">${esc(item.status)}</span></div><p>${esc(item.reason)}</p><code>Employee / record: ${esc(item.record_id)}</code>${cmp || fieldsTable(item)}${suggestion}${candidates && item.status === "open" ? `<fieldset class="target-options"><legend>Pick a value</legend>${candidates}</fieldset>` : ""}${item.context?.policy ? `<p class="policy-note">${esc(item.context.policy)}</p>` : ""}${item.status === "open" ? `${correction}<div class="review-actions">${buttons}</div>` : ""}</article>`;
}

function reviewCard(item) {
  if (item.actions) return recordCard(item);
  const samples = item.context?.sample_rows || [];
  const candidates = item.candidates || [];
  const options = candidates.map((value, index) => `<label class="target-option"><input type="radio" name="mapping-${esc(item.id)}" value="${esc(value)}" ${index === 0 ? "checked" : ""}><span>${esc(value)}</span></label>`).join("");
  const missingField = item.type === "missing_required_target";
  const actions = missingField
    ? `<button data-id="${esc(item.id)}" data-action="rejected">Acknowledge / quarantine affected records</button>`
    : `<button data-id="${esc(item.id)}" data-action="approved">Apply selected mapping</button><button data-id="${esc(item.id)}" data-action="rejected">Reject / quarantine column</button>`;
  return `<article class="review"><div class="review-header"><b>${esc(item.type.replaceAll("_", " "))}</b><span class="badge">${esc(item.status)}</span></div><p>${esc(item.reason)}</p><code>${item.scope === "record" ? "Employee / record" : "Source column"}: ${esc(item.record_id || item.field)}</code>${samples.length ? `<div class="data-preview"><small>Affected record and value</small>${samples.map(preview).join("")}</div>` : ""}${options ? `<fieldset class="target-options"><legend>Select the target field</legend>${options}</fieldset>` : ""}${item.context?.policy ? `<p class="policy-note">${esc(item.context.policy)}</p>` : ""}${item.status === "open" ? `<div class="review-actions">${actions}</div>` : ""}</article>`;
}

function bindReviewActions() {
  document.querySelectorAll("[data-id]").forEach((button) => {
    button.onclick = async () => {
      const item = run.escalations.find((entry) => entry.id === button.dataset.id);
      const chosen = document.querySelector(`input[name="mapping-${item.id}"]:checked`);
      const card = button.closest(".review");
      const typed = card.querySelector(".correct-value")?.value.trim();
      const field = card.querySelector(".correct-field")?.value || null;
      if (item.actions && button.dataset.action === "corrected" && !typed && !chosen) { $("notice").textContent = "Enter the corrected value first."; return; }
      if (!item.actions && item.candidates?.length && !chosen) { $("notice").textContent = "Select a target field before applying this mapping."; return; }
      try { run = await api(`/api/runs/${run.id}/escalations/${item.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: button.dataset.action, selected_value: button.dataset.action === "corrected" ? (typed || chosen?.value || null) : (chosen?.value || null), field }) }); render(); }
      catch (error) { $("notice").textContent = error.message; }
    };
  });
}

function render() {
  if (!run) return;
  $("run-section").hidden = false;
  const open = run.escalations.filter((entry) => entry.status === "open");
  $("summary").innerHTML = [["Run status", run.status.replaceAll("_", " ")], ["Source files", run.source_files.length || "Demo"], ["Reconciled records", run.records.length], ["Open reviews", open.length]].map(([label, value]) => `<div class="metric"><small>${esc(label)}</small><strong>${esc(value)}</strong></div>`).join("");
  $("events").innerHTML = run.events.map((event) => `<li><b>${esc(event.stage.replaceAll("_", " "))}</b>${esc(event.message)}</li>`).join("");
  const agent = run.agent || {};
  $("agent-badge").textContent = agent.mode === "gemini" ? `Gemini · ${agent.model}` : "Fallback mode";
  $("agent-badge").className = `badge ${agent.mode === "gemini" ? "gemini" : ""}`;
  $("review-count").textContent = open.length;
  $("reviews").innerHTML = run.escalations.map(reviewCard).join("") || "<p>No review items.</p>";
  $("outcomes").innerHTML = (run.outcomes || []).map((outcome) => `<span class="outcome ${esc(outcome.status)}"><b>${esc(outcome.employee_id || "record")}</b>${esc(outcome.status)}${outcome.reason ? ` · ${esc(outcome.reason)}` : ""}</span>`).join("") || "Records have not been pushed.";
  $("push").disabled = run.status !== "ready_to_push";
  $("retry").disabled = run.status !== "push_failed";
  $("rollback").disabled = !["completed", "completed_with_review", "push_failed"].includes(run.status);
  bindReviewActions();
}

async function start(demo) {
  try { $("start").disabled = true; const formData = new FormData(); if (!demo) [...$("files").files].forEach((file) => formData.append("files", file)); run = await api("/api/runs", { method: "POST", body: formData }); $("notice").textContent = "Agent run paused only where human judgment is required."; render(); }
  catch (error) { $("notice").textContent = error.message; }
  finally { $("start").disabled = false; }
}

$("files").onchange = showFiles;
$("start").onclick = () => start(false);
$("use-demo").onclick = () => start(true);
$("push").onclick = async () => { run = await api(`/api/runs/${run.id}/push`, { method: "POST" }); render(); };
$("retry").onclick = async () => { run = await api(`/api/runs/${run.id}/push?retry=true`, { method: "POST" }); render(); };
$("rollback").onclick = async () => { run = await api(`/api/runs/${run.id}/rollback`, { method: "POST" }); render(); };
api("/api/schema").then((schema) => { $("schema-name").textContent = schema.title.replace(" migration target schema", ""); $("schema-fields").textContent = `${Object.keys(schema.properties).length} target fields · ${schema.required.length} required`; }).catch((error) => { $("notice").textContent = error.message; });

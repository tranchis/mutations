"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const AXES = [
  ["relationship", "Relationship to the work", "Does it sustain or open a meaningful connection to the source?"],
  ["departure", "Productive departure", "Does its difference offer a direction worth pursuing?"],
  ["specificity", "Specificity", "Is there a particular detail, tension, or event worth attending to?"],
];
let generations = [], current = null, draft = null, detailID = null, files = [], busy = false;
let toastTimer, liveSource;

function localRead(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function localWrite(key, value) {
  try { localStorage.setItem(key, value); } catch { toast("Browser draft storage is unavailable. Submit feedback before leaving.", true); }
}
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 9000 : 4000);
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const {timeout: timeoutMs = 12000, ...fetchOptions} = options;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {...fetchOptions, signal:controller.signal});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Request failed (${response.status}).`);
    return value;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`${path} timed out`);
    throw error;
  } finally { clearTimeout(timeout); }
}
const post = (path, value) => api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)});
async function getWithRetry(path, attempts = 4) {
  let error;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try { return await api(path); }
    catch (caught) {
      error = caught;
      if (attempt + 1 < attempts) await new Promise((resolve) => setTimeout(resolve, 750 * 2 ** attempt));
    }
  }
  throw error;
}
async function postIdempotent(path, value, attempts = 3) {
  let error;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try { return await post(path, value); }
    catch (caught) {
      error = caught;
      if (attempt + 1 < attempts) await new Promise((resolve) => setTimeout(resolve, 750 * 2 ** attempt));
    }
  }
  throw error;
}

function freshDraft(generation) {
  const review = generation.review;
  return {
    expected_revision: review?.revision || 0,
    candidates: generation.candidates.map((candidate) => {
      const observation = review?.candidates.find((row) => row.candidate_id === candidate.id);
      return observation ? structuredClone(observation) : {
        candidate_id: candidate.id, decision:"undecided",
        scores:{relationship:null, departure:null, specificity:null}, note:"",
      };
    }),
    favourite_id: review?.favourite_id || null,
    display_order: review?.display_order ? [...review.display_order] : generation.candidates.map((c) => c.id),
    reviewer: review?.reviewer || $("#reviewer").value,
    note: review?.note || "", dirty:false,
  };
}
function remember() {
  if (!current || !draft) return;
  draft.dirty = true;
  draft.reviewer = $("#reviewer").value;
  localWrite(`mutations:draft:${current.id}`, JSON.stringify(draft));
  renderStatus();
}
function observation(id) { return draft.candidates.find((row) => row.candidate_id === id); }
function candidate(id) { return current.candidates.find((row) => row.id === id); }
function canReview() { return !current?.generation || current.generation.status === "ready"; }

function openGeneration(id) {
  current = generations.find((g) => g.id === id);
  if (!current) return;
  draft = freshDraft(current);
  const saved = localRead(`mutations:draft:${id}`);
  if (saved) {
    try {
      const restored = JSON.parse(saved);
      const ids = new Set(current.candidates.map((c) => c.id));
      if (restored.dirty && restored.expected_revision === draft.expected_revision
          && restored.candidates?.length === 9 && restored.candidates.every((c) => ids.has(c.candidate_id))
          && restored.display_order?.length === 9 && new Set(restored.display_order).size === 9
          && restored.display_order.every((cid) => ids.has(cid))) draft = restored;
      else if (restored.dirty) toast("A newer review exists. Your older draft is still in browser storage.");
    } catch { /* Ignore an invalid browser draft; server records remain authoritative. */ }
  }
  $("#reviewer").value = draft.reviewer;
  $("#generation-note").value = draft.note;
  $("#generation-note").disabled = false;
  $("#generation-title").textContent = current.title;
  $("#generation-number").textContent = String(current.ordinal).padStart(2, "0");
  $("#generation-subtitle").textContent = current.demo ? `Round ${current.demo.round} · choose what interests you, then continue` : `${current.experiment} · 9 candidates · select one, several, or none`;
  $("#example-banner").hidden = !current.demo;
  $("#example-banner").innerHTML = '<span>LIVE LOCAL GENERATION</span> Each candidate is rendered from its own image recipe and seed.';
  $("#source-toggle").disabled = false;
  $("#source-prompt").textContent = current.prompt || "No source prompt provided.";
  $("#export-feedback").href = current.is_demo ? "/api/export?include_demo=1" : "/api/export";
  $("#export-feedback").innerHTML = `${current.is_demo ? "Export demo session" : "Export feedback"} <span aria-hidden="true">↓</span>`;
  $("#reject-all").disabled = false;
  $("#shuffle").disabled = false;
  localWrite("mutations:current", current.id);
  renderNavigation(); renderGrid(); renderStatus();
  connectLive(current.id);
}

async function loadGeneration(id) {
  const existing = generations.find((generation) => generation.id === id);
  if (existing?.candidates) return openGeneration(id);
  try {
    const loaded = await getWithRetry(`/api/generations/${id}`);
    generations = generations.map((generation) => generation.id === id ? loaded : generation);
    openGeneration(id);
  } catch (error) { toast(`Could not load this generation. ${error.message}`, true); }
}

function renderNavigation() {
  $("#generation-count").textContent = generations.length;
  $("#generation-list").innerHTML = generations.length ? generations.map((g) => `
    <button class="generation-nav ${g.id === current?.id ? "active" : ""}" data-generation="${g.id}" ${g.id === current?.id ? 'aria-current="page"' : ""}>
      <span class="nav-number">${String(g.ordinal).padStart(2, "0")}</span>
      <span class="nav-title">${escapeHTML(g.title)}<span class="nav-detail">${g.is_demo ? "Archive · " : ""}${g.generation && g.generation.status !== "ready" ? `${escapeHTML(g.generation.status)} · ${g.generation.completed}/9` : g.review ? `Reviewed · revision ${g.review.revision}` : "Awaiting selection"}</span></span>
      ${g.review ? '<span class="nav-check" aria-label="Reviewed">✓</span>' : ""}
    </button>`).join("") : '<p class="sidebar-empty">Your generations will appear here.</p>';
}

function mediaHTML(item, detail = false) {
  if (!item.media) return "";
  const url = escapeHTML(detail ? item.media.url : item.media.preview_data || item.media.preview_url || item.media.url);
  return item.media.mime.startsWith("video/")
    ? `<video src="${url}" ${detail ? "controls" : "muted"} playsinline preload="metadata" aria-label="Candidate video"></video>`
    : `<img src="${url}" alt="${detail ? "Candidate output for review" : "Candidate visual output"}" draggable="false">`;
}
function identityHTML(item) {
  const g = item.genome || {}, generator = g.generator || {};
  const model = generator.model_id || g.model_id || generator.model || g.model || "unspecified model";
  const adapters = (generator.adapters || g.adapters || []).map((a) => a.model || a).join(", ") || "none";
  const operation = (g.program || []).map((step) => step.op).join(" → ") || "generate";
  const parents = item.parents?.length ? ` · parents ${item.parents.join(", ")}` : " · initial sample";
  const genome = escapeHTML(JSON.stringify(g, null, 2));
  return `<div class="identity-main">${escapeHTML(model)}</div><div class="identity-detail">adapter: ${escapeHTML(adapters)} · operation: ${escapeHTML(operation)} · seed: ${escapeHTML(g.seed ?? "—")}</div><div class="identity-detail">${escapeHTML(parents)}</div><details class="genome-details"><summary>Full genome</summary><pre>${genome}</pre></details>`;
}
function decisionsHTML(id, detailed = false) {
  const value = observation(id).decision;
  return [["keep","＋","Keep"],["reject","−","Reject"],["undecided","?",detailed ? "Undecided" : ""]].map(([decision,symbol,label]) => `
    <button class="decision" data-candidate="${escapeHTML(id)}" data-decision="${decision}" aria-pressed="${value === decision}" aria-label="${label || "Undecided"}" title="${label || "Leave undecided"}" ${canReview() ? "" : "disabled"}><span aria-hidden="true">${symbol}</span>${label}</button>`).join("");
}
function cardHTML(id, index, reveal) {
  const item = candidate(id), row = observation(id);
  if (!item.media) return `<article class="candidate-card empty-card" data-card="${escapeHTML(id)}"><div class="media-container"><span class="empty-index">${String(index + 1).padStart(2,"0")}</span><span class="empty-label">${current.generation?.status === "paused" ? "PAUSED" : "WAITING TO GENERATE"}</span></div><div class="card-controls">New image · seed ${escapeHTML(item.genome.seed)}</div></article>`;
  const annotated = row.note || Object.values(row.scores).some((score) => score !== null);
  return `<article class="candidate-card ${row.decision === "keep" ? "kept" : row.decision === "reject" ? "rejected" : ""}" data-card="${escapeHTML(id)}" aria-label="Candidate ${index + 1}: ${row.decision}">
    <div class="media-container">${mediaHTML(item)}<button class="media-open" data-inspect="${escapeHTML(id)}" aria-label="Inspect candidate ${index + 1}"></button><span class="candidate-index">${String(index + 1).padStart(2,"0")}</span><button class="media-expand" data-inspect="${escapeHTML(id)}" aria-label="Enlarge candidate ${index + 1}">↗</button>${item.media.mime.startsWith("video/") ? '<span class="video-label">▶ VIDEO</span>' : ""}${draft.favourite_id === id ? '<span class="favourite-badge">★ FAVOURITE</span>' : ""}</div>
    ${reveal ? `<div class="card-identity">${identityHTML(item)}</div>` : ""}
    <div class="card-controls"><div class="decisions">${decisionsHTML(id)}</div><button class="card-note ${annotated ? "annotated" : ""}" data-inspect="${escapeHTML(id)}" aria-label="${annotated ? "Edit" : "Add"} scores and notes for candidate ${index + 1}" title="Scores & notes">≡</button></div>
  </article>`;
}
function renderGrid() {
  if (!current) {
    $("#candidate-grid").innerHTML = Array.from({length:9}, (_, i) => `
      <article class="candidate-card empty-card"><div class="media-container"><span class="empty-index">${String(i + 1).padStart(2,"0")}</span><span class="empty-label">PREPARING GENERATION</span></div><div class="card-controls">Preparing your first nine</div></article>`).join("");
    $("#generation-note").disabled = true;
    $("#shuffle").disabled = true;
    return;
  }
  $("#candidate-grid").innerHTML = draft.display_order.map((id, index) => cardHTML(id, index, $("#reveal-metadata").checked)).join("");
}
function renderStatus() {
  if (!current) return;
  const kept = draft.candidates.filter((c) => c.decision === "keep").length;
  const rejected = draft.candidates.filter((c) => c.decision === "reject").length;
  const undecided = 9 - kept - rejected;
  const live = current.demo?.mode === "local-diffusion";
  $("#selection-count").innerHTML = `<span class="keep-dot"></span> ${kept} to keep <span class="count-divider">/</span> ${rejected} rejected <span class="count-divider">/</span> ${undecided} undecided`;
  $("#submit-review").disabled = busy || !canReview() || (!draft.dirty && !!current.review && !(live && (kept || undecided)));
  const action = live ? (kept || undecided ? "Save & generate nine" : "Save & pause") : `${current.review ? "Update" : "Submit"} feedback`;
  $("#submit-review").innerHTML = busy ? "Saving your choices…" : `${action} <span aria-hidden="true">↗</span>`;
  const status = $("#review-status");
  status.className = `pill ${draft.dirty ? "draft" : current.review ? "saved" : ""}`;
  status.textContent = !canReview() ? `${current.generation.status} · ${current.generation.completed}/9` : draft.dirty ? "Draft" : current.review ? `Saved · v${current.review.revision}` : "Awaiting selection";
  $("#save-message").textContent = !canReview() ? "Selection opens once all nine new images are ready." : live ? (current.review && !draft.dirty && !kept ? "Search continues only when you change the review or start a new session." : kept ? "Kept recipes are prioritized; undecided recipes remain neutral parents." : undecided ? "No Keeps selected: undecided recipes continue neutrally; rejects are excluded." : "All nine rejected: the search pauses. Choose Keep or clear a Reject to continue.") : draft.dirty ? "Draft saved in this browser. Submit to record your feedback." : current.review ? "Feedback recorded. Undecided candidates remain unlabeled." : "Undecided candidates remain unlabeled. Keys 1–9 toggle keep; Shift + 1–9 reject.";
  $("#reject-all").disabled = !canReview();
  renderJob();
}
function renderJob() {
  const job = current?.generation;
  $("#generation-progress").hidden = !job || job.status === "ready";
  if (!job) return;
  $("#job-message").textContent = job.message;
  $("#job-count").textContent = `${job.completed} / 9`;
  $("#job-progress").value = job.completed + (job.current_slot >= job.completed && job.steps ? job.step / job.steps : 0);
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";
  $("#pause-generation").hidden = !["queued","preparing","generating"].includes(job.status);
  $("#retry-generation").hidden = !["error","paused"].includes(job.status);
  $("#retry-generation").textContent = job.status === "error" ? "Retry generation" : "Resume generation";
}
function decide(id, decision) {
  if (!canReview() || busy) return;
  const row = observation(id);
  row.decision = row.decision === decision ? "undecided" : decision;
  if (draft.favourite_id === id && row.decision !== "keep") draft.favourite_id = null;
  remember(); renderGrid();
  if ($("#detail-dialog").open) renderDetailControls();
}

function inspect(id) {
  detailID = id;
  const item = candidate(id);
  if (!item.media) return;
  $("#detail-title").textContent = `Candidate ${String(draft.display_order.indexOf(id) + 1).padStart(2,"0")}`;
  $("#detail-preview").innerHTML = mediaHTML(item, true);
  $("#candidate-note").value = observation(id).note;
  $("#genome-json").textContent = JSON.stringify({id:item.id, genome:item.genome, parents:item.parents, operators:item.operators, execution:item.execution, selection_parents:item.selection_parents, provenance:item.provenance}, null, 2);
  $("#genome-details").hidden = !$("#reveal-metadata").checked;
  renderDetailControls();
  $("#detail-dialog").showModal();
}
function renderDetailControls() {
  $("#detail-decisions").innerHTML = decisionsHTML(detailID, true) + `<button class="favourite-button" id="favourite" aria-pressed="${draft.favourite_id === detailID}" title="Your preferred candidate among those you keep" ${canReview() ? "" : "disabled"}>★ ${draft.favourite_id === detailID ? "Favourite" : "Make favourite"}</button>`;
  $("#rubric").innerHTML = AXES.map(([key,label,help]) => `<div class="rubric-axis"><div class="axis-title">${label}</div><p class="axis-help">${help}</p><div class="score-options" role="group" aria-label="${label}">${[0,1,2,3,4].map((value) => `<button class="score-button" data-axis="${key}" data-score="${value}" aria-label="${label}: ${value}" aria-pressed="${observation(detailID).scores[key] === value}">${value}</button>`).join("")}<button class="score-clear" data-clear-axis="${key}">Clear</button></div></div>`).join("");
}

function openNewDialog() {
  $("#upload-error").textContent = "";
  $("#new-dialog").showModal();
}
function selectFiles(values) {
  files = Array.from(values).sort((a,b) => a.name.localeCompare(b.name, undefined, {numeric:true}));
  $("#file-count").textContent = `${files.length} / 9 files`;
  $("#file-list").innerHTML = files.map((file) => `<li>${escapeHTML(file.name)}</li>`).join("");
  $("#create-generation").disabled = files.length !== 9;
  $("#upload-error").textContent = files.length && files.length !== 9 ? "Choose exactly nine files for this generation." : "";
}
async function createGeneration(event) {
  event.preventDefault();
  const button = $("#create-generation");
  const closeButtons = $("#new-dialog").querySelectorAll("[data-close]");
  button.disabled = true;
  closeButtons.forEach((b) => { b.disabled = true; });
  $("#upload-error").textContent = "";
  try {
    if (files.length !== 9) throw new Error("Choose exactly nine files.");
    if (files.some((file) => !file.size || file.size > 128 * 1024 * 1024)) throw new Error("Each file must be non-empty and no larger than 128 MB.");
    let manifest = null;
    if ($("#manifest-file").files[0]) {
      manifest = JSON.parse(await $("#manifest-file").files[0].text());
      if (!manifest || typeof manifest !== "object" || !Array.isArray(manifest.candidates) || manifest.candidates.length !== 9) throw new Error("The metadata manifest needs exactly nine candidates.");
      const names = new Set(files.map((file) => file.name));
      const baseName = (path) => String(path || "").split(/[\\/]/).pop();
      if (names.size !== 9 || manifest.candidates.some((c) => !c || !names.has(baseName(c.file)))
          || new Set(manifest.candidates.map((c) => baseName(c.file))).size !== 9) throw new Error("Match each manifest candidate’s file to one unique uploaded filename.");
    }
    const assets = new Map();
    for (const [index,file] of files.entries()) {
      button.textContent = `Uploading ${index + 1} of 9…`;
      const asset = await api("/api/assets", {method:"POST", headers:{"Content-Type":"application/octet-stream","X-Filename":encodeURIComponent(file.name)}, body:file});
      assets.set(file.name, asset.asset_id);
    }
    const candidates = manifest ? manifest.candidates.map((c) => ({...c, asset_id:assets.get(c.file.split(/[\\/]/).pop())})) : files.map((file, i) => ({id:`candidate-${String(i+1).padStart(2,"0")}`, label:file.name, asset_id:assets.get(file.name), genome:{}, parents:[]}));
    const generation = await post("/api/generations", {title:$("#new-title").value.trim(), prompt:$("#new-prompt").value.trim() || manifest?.prompt || "", experiment:manifest?.experiment || "My Word", candidates});
    generations.unshift(generation);
    openGeneration(generation.id);
    $("#new-dialog").close();
    $("#new-form").reset(); selectFiles([]);
    toast("Nine candidates ready. Choose what to take further.");
  } catch (error) {
    $("#upload-error").textContent = error.message;
  } finally {
    button.innerHTML = 'Open generation <span aria-hidden="true">↗</span>';
    button.disabled = files.length !== 9;
    closeButtons.forEach((b) => { b.disabled = false; });
  }
}

async function submitReview() {
  if (!current || busy) return;
  const generationID = current.id;
  const submittedDraft = JSON.stringify(draft);
  const body = structuredClone(draft);
  body.reviewer = $("#reviewer").value;
  busy = true; renderStatus();
  $(".workspace").inert = true;
  $(".sidebar").inert = true;
  try {
    const generation = generations.find((g) => g.id === generationID);
    const review = draft.dirty || !generation.review ? await post(`/api/generations/${generationID}/reviews`, body) : generation.review;
    generation.review = review;
    if (current.id === generationID) {
      const unchanged = JSON.stringify(draft) === submittedDraft;
      draft.expected_revision = review.revision;
      draft.dirty = !unchanged;
      localWrite(`mutations:draft:${generationID}`, JSON.stringify(draft));
      renderNavigation();
    }
    if (generation.demo?.mode === "local-diffusion" && review.candidates.some((row) => row.decision !== "reject")) {
      const next = await post(`/api/generations/${generationID}/next`, {expected_revision:review.revision});
      if (!generations.some((g) => g.id === next.id)) generations.unshift(next);
      openGeneration(next.id);
      toast("Your choices are saved. Generating nine new descendants from the selected recipes.");
    } else {
      toast(generation.demo?.mode === "local-diffusion" ? "Choices saved. The search is paused; start a new session whenever you wish." : "Feedback saved. Your choices are ready for preference learning.");
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy = false;
    $(".workspace").inert = false;
    $(".sidebar").inert = false;
    renderStatus();
  }
}

async function startDemo(preset = "overview") {
  if (busy) return;
  busy = true;
  $("#new-generation").disabled = true;
  try {
    const modelId = $("#model-choice")?.value || "all";
    const generation = await postIdempotent("/api/demo", {preset, request_id:crypto.randomUUID(), ...(modelId === "all" ? {} : {model_ids: [modelId]})});
    generations.unshift(generation);
    openGeneration(generation.id);
    $("#source-panel").hidden = true;
    $("#source-toggle").setAttribute("aria-expanded", "false");
    toast("A new session is queued. Generating nine images locally.");
  } catch (error) { toast(error.message, true); }
  finally { busy = false; $("#new-generation").disabled = false; renderStatus(); }
}

document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]");
  if (close) return $(`#${close.dataset.close}`).close();
  const nav = event.target.closest("[data-generation]");
  if (nav) return loadGeneration(nav.dataset.generation);
  const preset = event.target.closest("[data-preset]");
  if (preset) return startDemo(preset.dataset.preset);
  const decision = event.target.closest("[data-decision]");
  if (decision) return decide(decision.dataset.candidate, decision.dataset.decision);
  const detail = event.target.closest("[data-inspect]");
  if (detail) return inspect(detail.dataset.inspect);
  if (event.target.closest("[data-add]")) return openNewDialog();
  const score = event.target.closest("[data-score]");
  if (score) {
    const row = observation(detailID), value = Number(score.dataset.score);
    row.scores[score.dataset.axis] = row.scores[score.dataset.axis] === value ? null : value;
    remember(); renderDetailControls(); renderGrid(); return;
  }
  const clear = event.target.closest("[data-clear-axis]");
  if (clear) { observation(detailID).scores[clear.dataset.clearAxis] = null; remember(); renderDetailControls(); renderGrid(); return; }
  if (event.target.closest("#favourite")) {
    if (draft.favourite_id === detailID) draft.favourite_id = null;
    else { observation(detailID).decision = "keep"; draft.favourite_id = detailID; }
    remember(); renderDetailControls(); renderGrid();
  }
});

$("#new-generation").addEventListener("click", () => startDemo());
$("#import-button").addEventListener("click", openNewDialog);
$("#new-form").addEventListener("submit", createGeneration);
$("#media-files").addEventListener("change", (event) => selectFiles(event.target.files));
$("#clear-files").addEventListener("click", () => { $("#media-files").value = ""; selectFiles([]); });
$("#new-dialog").addEventListener("cancel", (event) => { if ($("#create-generation").textContent.startsWith("Uploading")) event.preventDefault(); });
const drop = $("#drop-zone");
for (const type of ["dragenter","dragover"]) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("dragging"); });
for (const type of ["dragleave","drop"]) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove("dragging"); });
drop.addEventListener("drop", (event) => selectFiles(event.dataTransfer.files));
$("#source-toggle").addEventListener("click", () => { const panel = $("#source-panel"); panel.hidden = !panel.hidden; $("#source-toggle").setAttribute("aria-expanded", String(!panel.hidden)); });
$("#reveal-metadata").addEventListener("change", renderGrid);
$("#generation-note").addEventListener("input", (event) => { if (draft) { draft.note = event.target.value; remember(); } });
$("#candidate-note").addEventListener("input", (event) => { observation(detailID).note = event.target.value; remember(); renderGrid(); });
$("#reviewer").value = localRead("mutations:reviewer") || "";
$("#reviewer").addEventListener("input", (event) => { localWrite("mutations:reviewer", event.target.value); remember(); });
$("#detail-dialog").addEventListener("close", () => { $("#detail-preview").querySelector("video")?.pause(); });
$("#shuffle").addEventListener("click", () => {
  if (!draft) return;
  for (let i = draft.display_order.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [draft.display_order[i], draft.display_order[j]] = [draft.display_order[j], draft.display_order[i]];
  }
  remember(); renderGrid(); toast("Display shuffled. Choices and notes stay with their candidates.");
});
$("#reject-all").addEventListener("click", () => {
  draft.candidates.forEach((row) => { row.decision = "reject"; });
  draft.favourite_id = null; remember(); renderGrid();
  toast("All nine marked reject in your draft. Submit to record this decision.");
});
$("#submit-review").addEventListener("click", submitReview);
for (const action of ["pause", "retry"]) {
  $(`#${action}-generation`).addEventListener("click", async (event) => {
    const id = current.id;
    event.currentTarget.disabled = true;
    try {
      const updated = await post(`/api/generations/${id}/${action}`, {});
      generations = generations.map((g) => g.id === id ? updated : g);
      if (current.id === id) { current = updated; renderGrid(); renderStatus(); connectLive(id); }
    } catch (error) { toast(error.message, true); }
    finally { $(`#${action}-generation`).disabled = false; }
  });
}
$("#demo-button").addEventListener("click", () => {
  $("#source-panel").hidden = false;
  $("#source-toggle").setAttribute("aria-expanded", "true");
  $("#source-panel").scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block:"start"});
});
document.addEventListener("keydown", (event) => {
  if (!current || busy || document.querySelector("dialog[open]") || event.ctrlKey || event.metaKey || event.altKey
      || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName)) return;
  const number = Number(event.code.replace("Digit", ""));
  if (number >= 1 && number <= 9) {
    event.preventDefault();
    const id = draft.display_order[number - 1];
    decide(id, event.shiftKey ? "reject" : observation(id).decision === "keep" ? "undecided" : "keep");
  }
});

function applySnapshot(updated) {
  if (!current || updated.id !== current.id) return;
  const previous = current;
  generations = generations.map((generation) => generation.id === updated.id ? updated : generation);
  current = updated;
  const grid = $("#candidate-grid");
  for (const item of updated.candidates) {
    const old = previous.candidates.find((candidate) => candidate.id === item.id);
    if (item.media?.url === old?.media?.url) continue;
    const index = draft.display_order.indexOf(item.id);
    if (index < 0 || !grid.children[index]) continue;
    const template = document.createElement("template");
    template.innerHTML = cardHTML(item.id, index, $("#reveal-metadata").checked);
    grid.children[index].replaceWith(template.content.firstElementChild);
  }
  renderNavigation(); renderStatus();
}

function connectLive(id) {
  liveSource?.close();
  liveSource = null;
  if (!current || current.id !== id || !["queued","preparing","generating"].includes(current.generation?.status)) return;
  const source = new EventSource(`/api/generations/${id}/events`);
  liveSource = source;
  source.addEventListener("snapshot", (event) => {
    if (source !== liveSource) return;
    const updated = JSON.parse(event.data);
    applySnapshot(updated);
    if (!["queued","preparing","generating"].includes(updated.generation?.status)) {
      source.close();
      if (liveSource === source) liveSource = null;
    }
  });
  source.addEventListener("progress", (event) => {
    if (source !== liveSource || current?.id !== id) return;
    current.generation = JSON.parse(event.data);
    generations = generations.map((generation) => generation.id === id ? {...generation, generation:current.generation} : generation);
    renderNavigation(); renderStatus();
  });
  source.onerror = () => {
    if (source === liveSource) $("#save-message").textContent = "Reconnecting to live generation updates…";
  };
}

async function start() {
  renderGrid();
  let delay = 750;
  while (true) {
    try {
      const state = await api("/api/bootstrap");
      generations = state.generations;
      if (state.generation) {
        generations = generations.map((generation) => generation.id === state.generation.id ? state.generation : generation);
      }
      const research = state.catalog, modelState = {models:state.models};
    $("#model-choice").innerHTML = `<option value="all">All registered models (heterogeneous round)</option>` + modelState.models.map((model) => `<option value="${escapeHTML(model.id)}">${escapeHTML(model.label)}</option>`).join("");
    $("#model-choice").value = "all";
    $("#research-presets").innerHTML = research.presets.map((preset) => `<button class="research-preset" data-preset="${escapeHTML(preset.id)}"><strong>${escapeHTML(preset.title)}</strong><span>${escapeHTML(preset.question)}</span><span aria-hidden="true">↗</span></button>`).join("");
    $("#prompt-library").innerHTML = research.prompts.map((prompt) => `<article><strong>${escapeHTML(prompt.id)}</strong><p>${escapeHTML(prompt.text)}</p></article>`).join("");
    renderNavigation();
      if (state.generation) {
      const remembered = localRead("mutations:current");
        const rememberedGeneration = generations.find((g) => g.id === remembered && g.candidates);
        openGeneration(rememberedGeneration?.id || state.generation.id);
      }
      return;
    } catch (error) {
      $("#generation-title").textContent = "Connecting to the selection room…";
      $("#generation-subtitle").textContent = `${error.message}. Retrying automatically.`;
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 8000);
    }
  }
}
start();

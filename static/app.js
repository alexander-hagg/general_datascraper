"use strict";

let pollTimer = null;
let currentJobId = null;

/* ---------- small helpers ---------- */

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Request failed");
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Request failed");
  return data;
}

function norm(text) {
  return (text || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function setBanner(msg, type) {
  const b = document.getElementById("banner");
  if (!msg) { b.classList.add("hidden"); b.textContent = ""; return; }
  b.textContent = msg;
  b.className = "banner " + (type || "info");
}

function setBusy(running) {
  document.getElementById("search-btn").disabled = running;
  document.getElementById("extract-btn").disabled = running;
  ["run-load", "run-refresh", "run-delete"].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = running;
  });
  if (!running) {
    resetCancelButton();
    updateRunButtons();
  }
}

function resetCancelButton() {
  const btn = document.getElementById("cancel-job");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Cancel job";
  btn.setAttribute("aria-label", "Cancel the running job");
  btn.classList.add("hidden");
}

/* ---------- Ollama status ---------- */

async function checkStatus() {
  const pill = document.getElementById("ollama-pill");
  pill.className = "pill pill-unknown";
  pill.textContent = "Checking Ollama…";
  try {
    const s = await getJSON("/api/status");
    const ok = s.ollama && s.ollama.ok;
    pill.textContent = ok ? "Ollama ready" : "Ollama not available";
    pill.className = "pill " + (ok ? "pill-ok" : "pill-bad");
    pill.title = (s.ollama && s.ollama.message) || "";
    if (!ok) setBanner(s.ollama.message, "error");
    if (s.job) {
      setBusy(true);
      showProgress(true);
      pollJob(s.job.id);
    }
  } catch (e) {
    pill.className = "pill pill-bad";
    pill.textContent = "Cannot reach the server";
  }
}

/* ---------- sources ---------- */

async function loadSources() {
  const list = document.getElementById("source-list");
  list.innerHTML = "";
  const sources = await getJSON("/api/sources");
  sources.forEach(s => {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = s.name;
    cb.checked = true;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(s.label));
    list.appendChild(label);
  });
}

/* ---------- settings ---------- */

// Read and validate the optional publication-year range from the Search card.
// The server validates again; this just gives an immediate, clear message.
function readYearRange() {
  const from = document.getElementById("year-from").value.trim();
  const to = document.getElementById("year-to").value.trim();
  if (from && !/^[0-9]{4}$/.test(from)) {
    return { error: "From year must be a four-digit number (e.g. 2015)." };
  }
  if (to && !/^[0-9]{4}$/.test(to)) {
    return { error: "To year must be a four-digit number (e.g. 2024)." };
  }
  if (from && to && parseInt(from, 10) > parseInt(to, 10)) {
    return { error: "Start year must not be after end year." };
  }
  return { year_from: from, year_to: to };
}

// Readable label used in status messages. Kept separate from the query text.
function formatYearRange(from, to) {
  const yf = String(from || "").trim();
  const yt = String(to || "").trim();
  if (yf && yt) return yf + "-" + yt;
  if (yf) return yf + " onwards";
  if (yt) return "up to " + yt;
  return "";
}

async function loadSettings() {
  const s = await getJSON("/api/settings");
  document.getElementById("set-email").value = s.email || "";
  document.getElementById("set-model").value = s.model || "";
  document.getElementById("set-max").value = s.max_results || 50;
  document.getElementById("max-results").value = s.max_results || 50;
  document.getElementById("year-from").value = s.year_from || "";
  document.getElementById("year-to").value = s.year_to || "";
  document.getElementById("local-folder").value = s.local_folder || "";
  document.getElementById("local-recursive").checked = !!s.local_recursive;
  const journals = s.excluded_journals;
  document.getElementById("excluded-journals").value =
    Array.isArray(journals) ? journals.join("\n") : (journals || "");
}

async function saveSettings() {
  const years = readYearRange();
  if (years.error) { setBanner(years.error, "error"); return; }
  try {
    const body = {
      email: document.getElementById("set-email").value.trim(),
      model: document.getElementById("set-model").value.trim(),
      max_results: parseInt(document.getElementById("set-max").value, 10) || 50,
      excluded_journals: document.getElementById("excluded-journals").value,
      year_from: years.year_from,
      year_to: years.year_to,
      local_folder: document.getElementById("local-folder").value.trim(),
      local_recursive: document.getElementById("local-recursive").checked,
    };
    await postJSON("/api/settings", body);
    document.getElementById("max-results").value = body.max_results;
    setBanner("Settings saved.", "ok");
    setTimeout(() => setBanner(""), 2000);
    checkStatus();
  } catch (e) {
    setBanner(e.message, "error");
  }
}

/* ---------- prompt history ---------- */

const HISTORY_KINDS = ["search", "goal", "columns"];
const historyEntries = { search: [], goal: [], columns: [] };
const HISTORY_PLACEHOLDERS = {
  search: { empty: "No previous searches yet", choose: "Choose a previous search…" },
  goal: { empty: "No previous goals yet", choose: "Choose a previous goal…" },
  columns: { empty: "No previous columns yet", choose: "Choose previous columns…" },
};

function truncateHistoryText(text, max) {
  if (text.length <= max) return text;
  return text.slice(0, Math.max(0, max - 1)).replace(/\s+$/, "") + "…";
}

// Compact, accessible label for one stored prompt. Search entries show the
// rendered query (or "(folder only)"), the other kinds their text.
function historyOptionLabel(entry) {
  const data = entry.data || {};
  let text = "";
  if (entry.kind === "search") {
    text = String(data.query || "").replace(/\s+/g, " ").trim();
    if (!text) text = "(folder only)";
  } else {
    text = String(data.text || "").replace(/\s+/g, " ").trim();
  }
  if (!text) text = "(empty)";
  text = truncateHistoryText(text, 70);
  const uses = Number(entry.uses) || 0;
  return uses > 1 ? text + " (used " + uses + "×)" : text;
}

function renderHistorySelect(kind, entries) {
  const select = document.getElementById("history-" + kind);
  if (!select) return;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  const labels = HISTORY_PLACEHOLDERS[kind];
  placeholder.textContent = entries.length ? labels.choose : labels.empty;
  select.innerHTML = "";
  select.appendChild(placeholder);
  entries.forEach(entry => {
    const option = document.createElement("option");
    option.value = String(entry.id || "");
    option.textContent = historyOptionLabel(entry);
    select.appendChild(option);
  });
  select.value = "";
  select.disabled = entries.length === 0;
  const clearBtn = document.getElementById("history-" + kind + "-clear");
  if (clearBtn) clearBtn.disabled = entries.length === 0;
}

async function refreshHistory(kind) {
  try {
    const data = await getJSON("/api/history?kind=" + encodeURIComponent(kind));
    if (Array.isArray(data.entries)) {
      historyEntries[kind] = data.entries;
      renderHistorySelect(kind, data.entries);
    }
  } catch (e) {
    // History is optional; a failed refresh must not disturb the main UI.
  }
}

async function loadHistory() {
  await Promise.all(HISTORY_KINDS.map(refreshHistory));
}

function setFieldValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value === null || value === undefined ? "" : String(value);
}

function flashBanner(message, type, ms) {
  setBanner(message, type);
  setTimeout(() => {
    const banner = document.getElementById("banner");
    if (banner && banner.textContent === message) setBanner("");
  }, ms || 3000);
}

// Restore everything that belongs to a stored search. The search is
// deliberately NOT started: the user can review the tree and press Search.
function applySearchHistory(entry) {
  const data = entry.data || {};
  const tree = data.query_tree;
  const usableTree = tree && typeof tree === "object" &&
    (tree.type === "group" || tree.type === "term");
  if (usableTree) {
    queryModel.restore(tree);
  } else {
    // Legacy entries predate the query tree: put their rendered query into a
    // single term rather than trying to parse boolean syntax in the browser.
    queryModel.restore({ type: "group", op: "AND", children: [] });
    const query = String(data.query || "").trim();
    if (query) {
      const first = queryModel.root.children[0];
      if (first && first.type === "term") first.text = query;
      else queryModel.addTerm(queryModel.root, query, "AND");
    }
  }
  renderQuery();

  if ("max_results" in data && data.max_results !== null && data.max_results !== "") {
    setFieldValue("max-results", data.max_results);
  }
  if ("year_from" in data) setFieldValue("year-from", data.year_from || "");
  if ("year_to" in data) setFieldValue("year-to", data.year_to || "");
  if ("excluded_journals" in data) {
    const journals = data.excluded_journals;
    setFieldValue(
      "excluded-journals",
      Array.isArray(journals) ? journals.join("\n") : (journals || "")
    );
  }
  if ("local_folder" in data) setFieldValue("local-folder", data.local_folder || "");
  if ("local_recursive" in data) {
    const recursive = document.getElementById("local-recursive");
    if (recursive) recursive.checked = !!data.local_recursive;
  }
  if (Array.isArray(data.sources)) {
    const wanted = new Set(data.sources.map(s => String(s)));
    document.querySelectorAll("#source-list input[type=checkbox]").forEach(cb => {
      cb.checked = wanted.has(cb.value);
    });
  }
  flashBanner("Restored previous search. Click Search papers to run it.", "info", 3500);
}

function applyTextHistory(entry, targetId) {
  const data = entry.data || {};
  setFieldValue(targetId, data.text || "");
}

function bindHistorySelect(kind, onPick) {
  const select = document.getElementById("history-" + kind);
  if (!select) return;
  select.addEventListener("change", () => {
    const entry = historyEntries[kind].find(e => String(e.id) === select.value);
    if (entry) onPick(entry);
    select.value = "";
  });
}

async function clearPromptHistory(kind) {
  try {
    const result = await postJSON("/api/history/clear", { kind });
    await refreshHistory(kind);
    const count = Number(result.cleared) || 0;
    const label = kind === "search" ? "search" : kind;
    flashBanner(
      "Cleared " + count + " previous " + label +
        (count === 1 ? " entry." : " entries."),
      "ok",
      2500
    );
  } catch (e) {
    setBanner(e.message, "error");
  }
}

function bindHistoryClear(kind) {
  const btn = document.getElementById("history-" + kind + "-clear");
  if (btn) btn.onclick = () => clearPromptHistory(kind);
}

/* ---------- run folders (savable/loadable application state) ---------- */

const runsState = { enabled: false, active: null, runs: [] };

function runTimestamp(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text
    .replace("T", " ")
    .replace("Z", "")
    .replace(/\.\d+.*$/, "")
    .slice(0, 16);
}

function runCountsLabel(counts) {
  const c = counts || {};
  const parts = [];
  const n = key => Number(c[key]) || 0;
  if (n("papers")) parts.push(n("papers") + " paper" + (n("papers") === 1 ? "" : "s"));
  if (n("extracted")) parts.push(n("extracted") + " row" + (n("extracted") === 1 ? "" : "s"));
  if (n("closed")) parts.push(n("closed") + " closed");
  if (n("figures")) parts.push(n("figures") + " figure" + (n("figures") === 1 ? "" : "s"));
  return parts.join(" · ");
}

function runOptionLabel(run) {
  const bits = [String(run.name || run.id || "Run")];
  const date = runTimestamp(run.updated_at || run.created_at);
  if (date) bits.push(date);
  const query = truncateHistoryText(
    String(run.rendered_query || run.query || "").replace(/\s+/g, " ").trim(),
    70
  );
  if (query) bits.push(query);
  const counts = runCountsLabel(run.counts);
  if (counts) bits.push(counts);
  return bits.join(" — ");
}

function updateRunButtons() {
  const select = document.getElementById("run-select");
  const selected = select ? select.value : "";
  const busy = !!currentJobId;
  const activeId = runsState.active ? runsState.active.id : "";
  const load = document.getElementById("run-load");
  const refresh = document.getElementById("run-refresh");
  const remove = document.getElementById("run-delete");
  if (load) load.disabled = busy || !selected;
  if (refresh) refresh.disabled = busy || !runsState.active || selected !== activeId;
  if (remove) remove.disabled = busy || !selected || selected === activeId;
}

function renderRuns() {
  const current = document.getElementById("run-current");
  if (current) {
    current.innerHTML = "";
    if (runsState.active) {
      current.appendChild(makeEl(
        "span",
        "run-current-name",
        runsState.active.name || runsState.active.id
      ));
      const meta = [
        runTimestamp(runsState.active.updated_at || runsState.active.created_at),
        runCountsLabel(runsState.active.counts),
      ].filter(Boolean).join(" · ");
      if (meta) current.appendChild(makeEl("span", "run-meta", meta));
    } else {
      current.appendChild(makeEl(
        "span",
        "run-empty",
        "No run loaded — the next Search starts a new run folder."
      ));
    }
  }

  const select = document.getElementById("run-select");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = runsState.runs.length
    ? "Choose a saved run…"
    : "No saved runs yet";
  select.appendChild(placeholder);
  runsState.runs.forEach(run => {
    const option = document.createElement("option");
    option.value = String(run.id || "");
    option.textContent = runOptionLabel(run);
    select.appendChild(option);
  });
  const ids = new Set(runsState.runs.map(run => String(run.id || "")));
  let desired = ids.has(previous) ? previous : "";
  if (!desired && runsState.active && ids.has(String(runsState.active.id))) {
    desired = String(runsState.active.id);
  }
  if (!desired && runsState.runs.length) desired = String(runsState.runs[0].id);
  select.value = desired;
  select.disabled = runsState.runs.length === 0;
  updateRunButtons();
}

async function loadRuns(options) {
  const opts = options || {};
  const card = document.getElementById("runs-card");
  if (!card) return null;
  let data;
  try {
    data = await getJSON("/api/runs");
  } catch (e) {
    card.classList.add("hidden");
    return null;
  }
  runsState.enabled = !!data.enabled;
  runsState.active = data.active || null;
  runsState.runs = Array.isArray(data.runs) ? data.runs : [];
  card.classList.toggle("hidden", !runsState.enabled);
  const hint = document.getElementById("run-search-hint");
  if (hint) hint.classList.toggle("hidden", !runsState.enabled);
  if (!runsState.enabled) return data;
  renderRuns();
  if (opts.applyActive && runsState.active) {
    try {
      const payload = await getJSON("/api/run");
      if (payload && payload.state) applyRunState(payload.state);
    } catch (e) {
      // State loading is optional: the form and results still work without it.
    }
  }
  return data;
}

async function loadSelectedRun() {
  const select = document.getElementById("run-select");
  const runId = select ? select.value : "";
  if (!runId) return;
  try {
    const data = await postJSON(
      "/api/runs/" + encodeURIComponent(runId) + "/load",
      {}
    );
    if (data.state) applyRunState(data.state);
    await loadResults();
    await loadRuns();
  } catch (e) {
    setBanner(e.message, "error");
  }
}

async function deleteSelectedRun() {
  const select = document.getElementById("run-select");
  const runId = select ? select.value : "";
  if (!runId) return;
  const run = runsState.runs.find(item => String(item.id) === runId);
  const name = run ? (run.name || run.id) : runId;
  if (!window.confirm(
    "Delete the run \"" + name + "\" and all of its files? This cannot be undone."
  )) return;
  try {
    await postJSON("/api/runs/" + encodeURIComponent(runId) + "/delete", {});
    await loadRuns();
    flashBanner("Deleted run \"" + name + "\".", "ok", 3000);
  } catch (e) {
    setBanner(e.message, "error");
  }
}

function refreshActiveRun() {
  if (!runsState.active) {
    setBanner("Load a run before refreshing its search.", "error");
    return;
  }
  doSearch({ merge: true });
}

// Restore everything saved in a run's state.json. Loading never starts a job:
// the user can review the form, then Search/Extract explicitly.
function applyRunState(state) {
  if (!state || typeof state !== "object") return;
  const query = (state.query && typeof state.query === "object") ? state.query : {};
  const tree = query.tree;
  const usableTree = tree && typeof tree === "object" &&
    (tree.type === "group" || tree.type === "term");
  if (usableTree) {
    queryModel.restore(tree, { preserveEmpty: true });
    restoreQuerySelection({
      selected_paths: query.selected_paths,
      children_selected_paths: query.children_selected_paths,
    });
    renderQuery();
  }

  if (Array.isArray(state.sources)) {
    const wanted = new Set(state.sources.map(source => String(source)));
    document.querySelectorAll("#source-list input[type=checkbox]").forEach(cb => {
      cb.checked = wanted.has(cb.value);
    });
  }
  if ("max_results" in state && state.max_results !== null && state.max_results !== "") {
    setFieldValue("max-results", state.max_results);
  }
  if ("year_from" in state) setFieldValue("year-from", state.year_from || "");
  if ("year_to" in state) setFieldValue("year-to", state.year_to || "");
  if ("excluded_journals" in state) {
    const journals = state.excluded_journals;
    setFieldValue(
      "excluded-journals",
      Array.isArray(journals) ? journals.join("\n") : (journals || "")
    );
  }
  if ("local_folder" in state) setFieldValue("local-folder", state.local_folder || "");
  if ("local_recursive" in state) {
    const recursive = document.getElementById("local-recursive");
    if (recursive) recursive.checked = !!state.local_recursive;
  }

  const extraction = (state.extraction && typeof state.extraction === "object")
    ? state.extraction
    : {};
  if ("goal" in extraction) setFieldValue("goal", extraction.goal || "");
  if ("columns" in extraction) {
    const columns = extraction.columns;
    setFieldValue("columns", Array.isArray(columns) ? columns.join(", ") : (columns || ""));
  }
  if (extraction.mode) {
    const modeSelect = document.getElementById("extraction-mode");
    if (modeSelect) modeSelect.value = extraction.mode === "table" ? "table" : "records";
  }

  // Email/model are non-secret app settings only. They are restored into the
  // Settings fields for this view but are NOT saved automatically.
  const settings = (state.settings && typeof state.settings === "object")
    ? state.settings
    : {};
  if (settings.email) setFieldValue("set-email", settings.email);
  if (settings.model) setFieldValue("set-model", settings.model);

  const name = state.name || state.id || "run";
  const date = runTimestamp(state.updated_at || state.created_at);
  flashBanner(
    "Loaded run \"" + name + "\"" + (date ? " (" + date + ")" : "") +
      ". Review the form, then Search or Extract — nothing has started.",
    "info",
    5000
  );
}

/* ---------- query builder (nested boolean groups) ---------- */

/*
 * Tree model
 * ----------
 * Nodes are either:
 *   { id: string, type: "term",  text: string, op: "AND"|"OR"|"NOT" }
 *   { id: string, type: "group", op: "AND"|"OR"|"NOT", children: Node[] }
 *
 * Every node gets a stable `id` when it is created. Selection state is keyed
 * by these ids (see the selection section below) so it survives re-renders and
 * tree mutations such as adding terms or wrapping nodes in a new group.
 * `op` is the connector to the node's PREVIOUS sibling. The first sibling's
 * `op` is unused. The root is a group whose own `op` is unused.
 *
 * Public API (exposed as window.queryModel):
 *   root                       - the root group node
 *   findParent(node)           - parent group of a node (or null)
 *   pathForNode(node)          - child-index path from root, or null
 *   nodeAtPath(path)           - node at a child-index path, or null
 *   addTerm(parent?, text?, op?) -> term node
 *   addGroup(parent?, op?)     -> group node
 *   removeNode(node)           -> boolean
 *   wrapNodes(nodes, op?)      -> new group node (nodes must share a parent)
 *   dissolveGroup(group)       -> boolean (children move into the parent)
 *   moveNode(node, direction)  -> boolean (direction -1 = up, +1 = down)
 *   setOperator(node, op)      - set a node's connector
 *   setText(node, text)        - set a term's text
 *   serialize()                -> the exact query string sent to /api/search
 *   quoteTerm(text)            -> quoting rule used by serialize()
 *   countTerms()               -> number of non-empty-agnostic term nodes
 *   snapshot()                 -> JSON-safe tree without node ids (history)
 *   restore(snapshot)          -> replace the tree from a stored snapshot
 */
const queryModel = (function () {
  let nextId = 0;

  function makeId() {
    nextId += 1;
    return "n" + nextId;
  }

  const root = { id: makeId(), type: "group", op: "AND", children: [] };

  function findParent(node, parent) {
    parent = parent || root;
    if (parent.children.indexOf(node) !== -1) return parent;
    for (const child of parent.children) {
      if (child.type === "group") {
        const found = findParent(node, child);
        if (found) return found;
      }
    }
    return null;
  }

  // Stable location of a node in the tree: an array of child indices starting
  // at the root. [] is the root itself; null means "not in this tree". Saved
  // selection paths use these indices, so the tree must be restored with
  // empty terms/groups preserved (see restore(..., {preserveEmpty:true})).
  function pathForNode(node) {
    if (!node || typeof node !== "object") return null;
    if (node === root) return [];
    function walk(group, base) {
      for (let index = 0; index < group.children.length; index += 1) {
        const child = group.children[index];
        const path = base.concat(index);
        if (child === node) return path;
        if (child.type === "group") {
          const found = walk(child, path);
          if (found) return found;
        }
      }
      return null;
    }
    return walk(root, []);
  }

  function nodeAtPath(path) {
    if (!Array.isArray(path)) return null;
    let node = root;
    for (const index of path) {
      if (!Number.isInteger(index) || index < 0) return null;
      if (!node || node.type !== "group" || index >= node.children.length) {
        return null;
      }
      node = node.children[index];
    }
    return node;
  }

  function addTerm(parent, text, op) {
    parent = parent || root;
    const node = { id: makeId(), type: "term", text: text || "", op: op || "AND" };
    parent.children.push(node);
    return node;
  }

  function addGroup(parent, op) {
    parent = parent || root;
    const node = { id: makeId(), type: "group", op: op || "AND", children: [] };
    parent.children.push(node);
    return node;
  }

  function removeNode(node) {
    const parent = findParent(node);
    if (!parent) return false;
    const i = parent.children.indexOf(node);
    if (i === -1) return false;
    parent.children.splice(i, 1);
    return true;
  }

  function wrapNodes(nodes, op) {
    if (!nodes || !nodes.length) return null;
    const parent = findParent(nodes[0]);
    if (!parent) return null;
    for (const n of nodes) {
      if (findParent(n) !== parent) return null;
    }
    const ordered = parent.children.filter(c => nodes.indexOf(c) !== -1);
    if (!ordered.length) return null;
    const firstIndex = parent.children.indexOf(ordered[0]);
    const group = { id: makeId(), type: "group", op: op || ordered[0].op || "AND", children: [] };
    ordered.forEach(n => {
      const idx = parent.children.indexOf(n);
      if (idx !== -1) parent.children.splice(idx, 1);
    });
    parent.children.splice(firstIndex, 0, group);
    ordered.forEach((n, i) => {
      if (i === 0) n.op = "AND"; // unused as first child
      group.children.push(n);
    });
    return group;
  }

  function dissolveGroup(group) {
    if (!group || group.type !== "group") return false;
    const parent = findParent(group);
    if (!parent) return false;
    const idx = parent.children.indexOf(group);
    const children = group.children.slice();
    if (children.length) children[0].op = group.op || "AND";
    parent.children.splice(idx, 1, ...children);
    return true;
  }

  function moveNode(node, direction) {
    const parent = findParent(node);
    if (!parent) return false;
    const i = parent.children.indexOf(node);
    const j = i + direction;
    if (j < 0 || j >= parent.children.length) return false;
    parent.children.splice(i, 1);
    parent.children.splice(j, 0, node);
    return true;
  }

  function setOperator(node, op) {
    if (node) node.op = op;
  }

  function setText(node, text) {
    if (node && node.type === "term") node.text = text;
  }

  // Multi-word terms are quoted unless they already contain double quotes or a
  // PubMed-style field tag (e.g. [Title/Abstract]). Single words stay bare.
  function quoteTerm(text) {
    const t = String(text || "").trim().replace(/\s+/g, " ");
    if (!t) return "";
    if (t.indexOf('"') !== -1) return t;
    if (/\[[^\]]+\]/.test(t)) return t;
    if (/\s/.test(t)) return '"' + t + '"';
    return t;
  }

  function serializeNode(node) {
    if (!node) return "";
    if (node.type === "term") return quoteTerm(node.text);
    return serializeGroup(node, false);
  }

  function serializeGroup(group, isRoot) {
    const parts = [];
    group.children.forEach(child => {
      const s = serializeNode(child);
      if (!s) return; // empty terms/groups are excluded
      if (parts.length === 0) parts.push(s);
      else parts.push((child.op || "AND") + " " + s);
    });
    const inner = parts.join(" ");
    if (isRoot) return inner;
    return inner ? "(" + inner + ")" : "";
  }

  function serialize() {
    return serializeGroup(root, true);
  }

  function countTerms() {
    let n = 0;
    (function walk(g) {
      g.children.forEach(c => {
        if (c.type === "term") n++;
        else walk(c);
      });
    })(root);
    return n;
  }

  function safeOperator(op) {
    return op === "OR" || op === "NOT" ? op : "AND";
  }

  // Plain JSON snapshot for the history store. Node ids are browser-only and
  // are deliberately omitted so the server can deduplicate equivalent trees.
  function snapshotNode(node) {
    if (!node || typeof node !== "object") return null;
    if (node.type === "term") {
      return {
        type: "term",
        text: String(node.text || ""),
        op: safeOperator(node.op),
      };
    }
    if (node.type === "group") {
      return {
        type: "group",
        op: safeOperator(node.op),
        children: (Array.isArray(node.children) ? node.children : [])
          .map(snapshotNode)
          .filter(Boolean),
      };
    }
    return null;
  }

  function snapshot() {
    return {
      type: "group",
      op: "AND",
      children: root.children.map(snapshotNode).filter(Boolean),
    };
  }

  // Append one stored node below `parent`, creating fresh ids through
  // addTerm/addGroup. By default empty terms/groups and malformed objects are
  // ignored (history restores only meaningful queries). `preserveEmpty` keeps
  // them, which is required when restoring a run: selection paths are child
  // indices and must still resolve to the same nodes.
  function appendSnapshot(raw, parent, preserveEmpty) {
    if (!raw || typeof raw !== "object") return false;
    if (raw.type === "term") {
      const text = typeof raw.text === "string" ? raw.text : "";
      if (!text.trim() && !preserveEmpty) return false;
      addTerm(parent, text, safeOperator(raw.op));
      return true;
    }
    if (raw.type !== "group") return false;
    const group = addGroup(parent, safeOperator(raw.op));
    let added = 0;
    (Array.isArray(raw.children) ? raw.children : []).forEach(child => {
      if (appendSnapshot(child, group, preserveEmpty)) added += 1;
    });
    if (!added && !preserveEmpty) {
      removeNode(group);
      return false;
    }
    return true;
  }

  function restore(snapshotTree, options) {
    const preserveEmpty = !!(
      options && typeof options === "object" && options.preserveEmpty
    );
    root.children = [];
    const snap = (snapshotTree && typeof snapshotTree === "object")
      ? snapshotTree
      : null;
    const children = !snap
      ? []
      : (snap.type === "term" ? [snap] : (Array.isArray(snap.children) ? snap.children : []));
    children.forEach(child => appendSnapshot(child, root, preserveEmpty));
    if (root.children.length === 0 && !preserveEmpty) addTerm(root);
    try {
      if (typeof selectedIds !== "undefined") selectedIds.clear();
      if (typeof childrenSelectedGroups !== "undefined") childrenSelectedGroups.clear();
    } catch (e) {
      // Selection state is not initialised (e.g. a DOM-free Node harness).
    }
    return root;
  }

  return {
    root,
    findParent,
    pathForNode,
    nodeAtPath,
    addTerm,
    addGroup,
    removeNode,
    wrapNodes,
    dissolveGroup,
    moveNode,
    setOperator,
    setText,
    serialize,
    quoteTerm,
    countTerms,
    snapshot,
    restore,
  };
})();

window.queryModel = queryModel;

/* ---------- selection state (id-based) ---------- */

/*
 * Selection is stored as a Set of node ids, never as DOM elements or object
 * references. Re-rendering the tree therefore restores the same selection, and
 * structural mutations (add term/group, wrapNodes) keep existing selections
 * because node ids survive the move.
 *
 * Group click cycle (see cycleGroupSelectionState below):
 *   state 2 absent   -> 1st click: the group node itself is selected
 *   state 1          -> 2nd click: all descendants are selected ("children
 *                       selected" state on the group, group id removed)
 *   state 2          -> 3rd click: back to state 1
 *   ... repeats 1 -> 2 -> 1 -> 2 ...
 */
const selectedIds = new Set();
const childrenSelectedGroups = new Set(); // group ids currently in state 2

function findNodeById(id) {
  let found = null;
  (function walk(group) {
    group.children.forEach(n => {
      if (found) return;
      if (n.id === id) found = n;
      else if (n.type === "group") walk(n);
    });
  })(queryModel.root);
  return found;
}

function collectDescendants(group) {
  const out = [];
  (function walk(g) {
    g.children.forEach(n => {
      out.push(n);
      if (n.type === "group") walk(n);
    });
  })(group);
  return out;
}

// Pure-ish state transition: mutates the selection sets, returns a small
// summary. Kept DOM-free so it can be tested from Node.
function cycleGroupSelectionState(group) {
  if (!group || group.type !== "group" || group === queryModel.root) {
    return null;
  }
  const id = group.id;
  const descendants = collectDescendants(group);
  if (selectedIds.has(id)) {
    // state 1 -> state 2: select every descendant, drop the group itself.
    selectedIds.delete(id);
    childrenSelectedGroups.add(id);
    descendants.forEach(n => selectedIds.add(n.id));
  } else if (childrenSelectedGroups.has(id)) {
    // state 2 -> state 1: clear descendants, select the group itself.
    childrenSelectedGroups.delete(id);
    descendants.forEach(n => selectedIds.delete(n.id));
    selectedIds.add(id);
  } else {
    // no state -> state 1: just this group; its members are not selected.
    descendants.forEach(n => selectedIds.delete(n.id));
    childrenSelectedGroups.delete(id);
    selectedIds.add(id);
  }
  return {
    groupSelected: selectedIds.has(id),
    childrenSelected: childrenSelectedGroups.has(id),
  };
}

function cycleGroupSelection(group) {
  const result = cycleGroupSelectionState(group);
  if (result) syncSelectionUI();
}

// Drop ids that no longer exist in the tree (e.g. after remove/dissolve).
function pruneSelection() {
  const alive = new Set();
  (function walk(group) {
    alive.add(group.id);
    group.children.forEach(n => {
      alive.add(n.id);
      if (n.type === "group") walk(n);
    });
  })(queryModel.root);
  [...selectedIds].forEach(id => { if (!alive.has(id)) selectedIds.delete(id); });
  [...childrenSelectedGroups].forEach(id => { if (!alive.has(id)) childrenSelectedGroups.delete(id); });
}

// Top-most selected nodes only: if a group and one of its descendants are both
// selected, the descendant is skipped so wrapNodes() receives siblings that
// share the same parent. This also makes a state-2 group wrap its members.
function collectSelectedTopLevel() {
  const out = [];
  (function walk(group, ancestorSelected) {
    group.children.forEach(n => {
      const selected = selectedIds.has(n.id);
      if (selected && !ancestorSelected) out.push(n);
      if (n.type === "group") walk(n, ancestorSelected || selected);
    });
  })(queryModel.root, false);
  return out;
}

function syncSelectionUI() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  document.querySelectorAll("#term-list .node-select").forEach(cb => {
    const node = cb.__node;
    if (!node) return;
    // A group in the "members selected" state keeps its own box checked so the
    // control reflects that the group as a whole is active.
    cb.checked = selectedIds.has(node.id) || childrenSelectedGroups.has(node.id);
  });
  document.querySelectorAll("#term-list .term-row").forEach(row => {
    const node = row.__node;
    if (!node) return;
    row.classList.toggle("term-selected", selectedIds.has(node.id));
  });
  document.querySelectorAll("#term-list .query-group").forEach(block => {
    const node = block.__node;
    if (!node) return;
    block.classList.toggle("group-selected", selectedIds.has(node.id));
    block.classList.toggle("group-children-selected", childrenSelectedGroups.has(node.id));
  });
}

// JSON-safe checkbox selection snapshot. Paths are child indices, so the saved
// state is independent of browser-generated node ids. A state-2 group also
// records its descendants in selected_paths (they are selected as a whole);
// both pieces are needed to recreate the same UI after a reload.
function snapshotQuerySelection() {
  const selected_paths = [];
  const children_selected_paths = [];
  selectedIds.forEach(id => {
    const path = queryModel.pathForNode(findNodeById(id));
    if (Array.isArray(path)) selected_paths.push(path);
  });
  childrenSelectedGroups.forEach(id => {
    const path = queryModel.pathForNode(findNodeById(id));
    if (Array.isArray(path)) children_selected_paths.push(path);
  });
  const sortPaths = paths => paths.sort((a, b) => {
    for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
      if (a[i] !== b[i]) return a[i] - b[i];
    }
    return a.length - b.length;
  });
  return {
    selected_paths: sortPaths(selected_paths),
    children_selected_paths: sortPaths(children_selected_paths),
  };
}

// Restore a saved selection after queryModel.restore(...). Invalid/malformed
// paths are ignored; they must never prevent the rest of the state from
// loading. The tree is expected to have been restored with preserveEmpty.
function restoreQuerySelection(selection) {
  selectedIds.clear();
  childrenSelectedGroups.clear();
  const data = (selection && typeof selection === "object") ? selection : {};

  const validPaths = value => (Array.isArray(value) ? value : [])
    .map(path => {
      if (!Array.isArray(path)) return null;
      const cleaned = [];
      for (const index of path) {
        if (!Number.isInteger(index) || index < 0) return null;
        cleaned.push(index);
      }
      return cleaned;
    })
    .filter(Boolean);

  validPaths(data.selected_paths).forEach(path => {
    const node = queryModel.nodeAtPath(path);
    if (node) selectedIds.add(node.id);
  });
  validPaths(data.children_selected_paths).forEach(path => {
    const node = queryModel.nodeAtPath(path);
    if (!node || node.type !== "group") return;
    childrenSelectedGroups.add(node.id);
    collectDescendants(node).forEach(child => selectedIds.add(child.id));
  });
  pruneSelection();
  syncSelectionUI();
}

/* ---------- query builder rendering ---------- */

function makeOpSelect(node, index, options) {
  if (index === 0) return null; // first sibling has no connector
  const op = document.createElement("select");
  op.className = "term-op";
  options.forEach(o => {
    const opt = document.createElement("option");
    opt.value = o;
    opt.textContent = o;
    if (node.op === o) opt.selected = true;
    op.appendChild(opt);
  });
  op.onchange = () => {
    queryModel.setOperator(node, op.value);
    updatePreview();
  };
  return op;
}

function makeSelectCheckbox(node) {
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "node-select";
  cb.title = "Select for grouping";
  cb.checked = selectedIds.has(node.id) || childrenSelectedGroups.has(node.id);
  cb.__node = node;
  cb.onchange = () => {
    if (cb.checked) {
      selectedIds.add(node.id);
      childrenSelectedGroups.delete(node.id);
    } else {
      // If this group was showing its members as selected, unchecking the box
      // clears that whole selection. Unchecking a plain group selection must
      // not touch any independent child selections.
      const wasMembersSelected = childrenSelectedGroups.has(node.id);
      selectedIds.delete(node.id);
      childrenSelectedGroups.delete(node.id);
      if (wasMembersSelected) {
        collectDescendants(node).forEach(n => selectedIds.delete(n.id));
      }
    }
    syncSelectionUI();
  };
  return cb;
}

/* ---------- instant autocomplete (PubMed, proxied by Flask) ---------- */

function attachAutocomplete(input, node) {
  const wrap = input.parentElement;
  const list = document.createElement("div");
  list.className = "autocomplete-list hidden";
  wrap.appendChild(list);

  let items = [];
  let active = -1;
  let timer = null;
  let reqId = 0;

  function close() {
    list.classList.add("hidden");
    list.innerHTML = "";
    items = [];
    active = -1;
  }

  function choose(value) {
    input.value = value;
    queryModel.setText(node, value);
    updatePreview();
    close();
    input.focus();
  }

  function highlight(i) {
    active = i;
    [...list.children].forEach((el, idx) => el.classList.toggle("active", idx === active));
  }

  function open(suggestions, source) {
    items = suggestions;
    active = -1;
    list.innerHTML = "";
    suggestions.forEach(s => {
      const item = document.createElement("div");
      item.className = "autocomplete-item";
      item.appendChild(makeEl("span", "autocomplete-text", s));
      const badge = makeSourceBadge(source || "pubmed");
      if (badge) item.appendChild(badge);
      // mousedown + preventDefault keeps focus so click selection works.
      item.onmousedown = (e) => {
        e.preventDefault();
        choose(s);
      };
      list.appendChild(item);
    });
    list.classList.toggle("hidden", suggestions.length === 0);
  }

  async function fetchSuggestions() {
    const q = input.value.trim();
    // Don't query for very short input, quoted phrases, or field-tagged terms.
    if (q.length < 2 || q.indexOf('"') !== -1 || /\[[^\]]+\]/.test(q)) {
      close();
      return;
    }
    const id = ++reqId;
    try {
      const data = await getJSON("/api/autocomplete?q=" + encodeURIComponent(q));
      if (id !== reqId) return;
      open(Array.isArray(data.suggestions) ? data.suggestions : [], data.source);
    } catch (e) {
      if (id === reqId) close(); // silent fallback: no suggestions
    }
  }

  input.addEventListener("input", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(fetchSuggestions, 300);
  });

  input.addEventListener("keydown", (e) => {
    if (list.classList.contains("hidden") || !items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      highlight((active + 1) % items.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      highlight(active <= 0 ? items.length - 1 : active - 1);
    } else if (e.key === "Enter") {
      if (active >= 0) {
        e.preventDefault();
        choose(items[active]);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  });

  // Blur covers outside clicks; mousedown on an item prevents the blur so the
  // click can be handled first.
  input.addEventListener("blur", () => setTimeout(close, 120));
}

/* ---------- LLM related-term suggestions ---------- */

function makeEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function sourceBadgeLabel(source, model) {
  if (source === "pubmed") return "PubMed";
  if (source === "llm") return model ? "LLM · " + model : "LLM";
  if (!source) return "";
  return String(source);
}

// Small provenance badge shown next to every suggested term/item.
function makeSourceBadge(source, model) {
  const label = sourceBadgeLabel(source, model);
  if (!label) return null;
  const badge = makeEl("span", "suggest-badge", label);
  badge.title = label;
  return badge;
}

function buildSuggestionChecklist(suggestions, source, model) {
  const box = makeEl("div", "suggest-checklist");
  suggestions.forEach(s => {
    const label = makeEl("label", "suggest-item");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.dataset.value = s;
    label.appendChild(cb);
    label.appendChild(makeEl("span", "suggest-text", s));
    const badge = makeSourceBadge(source, model);
    if (badge) label.appendChild(badge);
    box.appendChild(label);
  });
  return box;
}

// Wrap an existing term into an OR group with the accepted synonyms.
// Uses queryModel.wrapNodes so the group appears exactly where the term was,
// even when the term is already nested inside another group.
function applyOrGroupToNode(node, suggestions) {
  if (!suggestions || !suggestions.length) return;
  const group = queryModel.wrapNodes([node], node.op || "AND");
  if (!group) {
    setBanner("Could not wrap this term into a group.", "error");
    return;
  }
  suggestions.forEach(s => queryModel.addTerm(group, s, "OR"));
}

function showPanelError(panel, message) {
  panel.querySelectorAll(".suggest-error").forEach(e => e.remove());
  panel.appendChild(makeEl("div", "suggest-error", message));
}

async function openTermSuggestPanel(node, row) {
  row.querySelectorAll(".suggest-panel").forEach(p => p.remove());
  const panel = makeEl("div", "suggest-panel");
  row.appendChild(panel);

  const term = (node.text || "").trim();
  if (!term) {
    showPanelError(panel, "Type a term first, then ask for related terms.");
    return;
  }

  panel.appendChild(makeEl("div", "suggest-loading", "Asking the LLM for related terms…"));
  try {
    const data = await postJSON("/api/suggest", { term });
    panel.innerHTML = "";
    const suggestions = data.suggestions || [];
    if (!suggestions.length) {
      panel.appendChild(makeEl("div", "suggest-empty", "No related terms were returned."));
      return;
    }
    panel.appendChild(makeEl("div", "suggest-head", "Related terms for \u201c" + term + "\u201d"));
    panel.appendChild(buildSuggestionChecklist(suggestions, data.source || "llm", data.model || ""));

    const actions = makeEl("div", "suggest-actions");
    const add = makeEl("button", "ghost-btn mini", "Add selected as OR-group");
    add.type = "button";
    add.onclick = () => {
      const selected = [...panel.querySelectorAll("input[type=checkbox]:checked")]
        .map(c => c.dataset.value);
      if (!selected.length) {
        showPanelError(panel, "Select at least one suggestion.");
        return;
      }
      applyOrGroupToNode(node, selected);
      renderQuery();
    };
    const cancel = makeEl("button", "ghost-btn mini", "Cancel");
    cancel.type = "button";
    cancel.onclick = () => panel.remove();
    actions.appendChild(add);
    actions.appendChild(cancel);
    panel.appendChild(actions);
  } catch (e) {
    panel.innerHTML = "";
    showPanelError(panel, e.message || "Could not get suggestions.");
    const dismiss = makeEl("button", "ghost-btn mini", "Dismiss");
    dismiss.type = "button";
    dismiss.onclick = () => panel.remove();
    panel.appendChild(dismiss);
  }
}

function collectTermNodes() {
  const out = [];
  (function walk(group) {
    group.children.forEach(n => {
      if (n.type === "term") out.push(n);
      else walk(n);
    });
  })(queryModel.root);
  return out;
}

function applyGlobalSelections(panel) {
  const map = new Map();
  panel.querySelectorAll(".suggest-group").forEach(block => {
    const term = norm(block.dataset.term);
    const values = [...block.querySelectorAll("input[type=checkbox]:checked")]
      .map(c => c.dataset.value);
    if (term && values.length) map.set(term, values);
  });
  if (!map.size) {
    showPanelError(panel, "Select at least one suggestion.");
    return;
  }
  collectTermNodes().forEach(node => {
    const values = map.get(norm(node.text));
    if (values && values.length) applyOrGroupToNode(node, values);
  });
  panel.classList.add("hidden");
  panel.innerHTML = "";
  renderQuery();
}

async function suggestAllTerms() {
  const panel = document.getElementById("global-suggest-panel");
  panel.classList.remove("hidden");
  panel.innerHTML = "";

  const nodes = collectTermNodes().filter(n => (n.text || "").trim());
  if (!nodes.length) {
    showPanelError(panel, "Add at least one search term first.");
    return;
  }
  const terms = [];
  const seen = new Set();
  nodes.forEach(n => {
    const t = n.text.replace(/\s+/g, " ").trim();
    const key = norm(t);
    if (!seen.has(key)) {
      seen.add(key);
      terms.push(t);
    }
  });

  panel.appendChild(makeEl("div", "suggest-loading", "Asking the LLM for related terms…"));
  try {
    const data = await postJSON("/api/suggest", { terms });
    panel.innerHTML = "";
    const groups = (data.groups || []).filter(g => (g.suggestions || []).length);
    if (!groups.length) {
      panel.appendChild(makeEl("div", "suggest-empty", "No related terms were returned."));
      return;
    }
    panel.appendChild(makeEl("div", "suggest-head", "Related terms"));
    groups.forEach(g => {
      const block = makeEl("div", "suggest-group");
      block.dataset.term = g.term;
      block.appendChild(makeEl("div", "suggest-group-term", g.term));
      block.appendChild(buildSuggestionChecklist(
        g.suggestions,
        g.source || data.source || "llm",
        g.model || data.model || ""
      ));
      panel.appendChild(block);
    });

    const actions = makeEl("div", "suggest-actions");
    const add = makeEl("button", "ghost-btn mini", "Add selected as OR-groups");
    add.type = "button";
    add.onclick = () => applyGlobalSelections(panel);
    const cancel = makeEl("button", "ghost-btn mini", "Cancel");
    cancel.type = "button";
    cancel.onclick = () => {
      panel.classList.add("hidden");
      panel.innerHTML = "";
    };
    actions.appendChild(add);
    actions.appendChild(cancel);
    panel.appendChild(actions);
  } catch (e) {
    panel.innerHTML = "";
    showPanelError(panel, e.message || "Could not get suggestions.");
    const dismiss = makeEl("button", "ghost-btn mini", "Dismiss");
    dismiss.type = "button";
    dismiss.onclick = () => {
      panel.classList.add("hidden");
      panel.innerHTML = "";
    };
    panel.appendChild(dismiss);
  }
}

/* ---------- LLM "Improve goal" (extraction instruction) ---------- */

function closeGoalSuggestPanel() {
  const panel = document.getElementById("goal-suggest-panel");
  if (!panel) return;
  panel.classList.add("hidden");
  panel.innerHTML = "";
}

function appendGoalPanelClose(panel, label) {
  const actions = makeEl("div", "suggest-actions");
  const close = makeEl("button", "ghost-btn mini", label || "Cancel");
  close.type = "button";
  close.onclick = closeGoalSuggestPanel;
  actions.appendChild(close);
  panel.appendChild(actions);
}

function applyGoalSuggestion(text) {
  document.getElementById("goal").value = text;
  closeGoalSuggestPanel();
  document.getElementById("goal").focus();
  setBanner("Goal updated. Review it, then click Extract data when you are ready.", "ok");
}

async function improveGoal() {
  const goalEl = document.getElementById("goal");
  const panel = document.getElementById("goal-suggest-panel");
  const button = document.getElementById("improve-goal");
  const goal = goalEl.value.trim();

  panel.classList.remove("hidden");
  panel.innerHTML = "";
  if (!goal) {
    showPanelError(panel, "Describe what data you want to extract first, then ask for an improved goal.");
    goalEl.focus();
    return;
  }

  const parsed = parseColumnInput(document.getElementById("columns").value);
  const mode = document.getElementById("extraction-mode").value || "records";
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "✨ Improving…";
  panel.appendChild(makeEl("div", "suggest-loading", "Asking the LLM for clearer goal wording…"));

  try {
    const data = await postJSON("/api/suggest-goal", {
      goal,
      columns: parsed.columns,
      mode,
    });
    panel.innerHTML = "";
    const suggestions = (data.suggestions || [])
      .filter(s => typeof s === "string" && s.trim());
    if (!suggestions.length) {
      panel.appendChild(makeEl(
        "div",
        "suggest-empty",
        "The LLM did not return a usable goal. Try rewording your goal or listing the columns you need."
      ));
      appendGoalPanelClose(panel, "Dismiss");
      return;
    }

    const head = makeEl("div", "suggest-head goal-suggest-head");
    head.appendChild(makeEl("span", "", "Improved goal candidates"));
    const badge = makeSourceBadge(data.source || "llm", data.model || "");
    if (badge) head.appendChild(badge);
    panel.appendChild(head);

    suggestions.forEach(s => {
      const block = makeEl("div", "goal-suggestion");
      block.appendChild(makeEl("div", "goal-suggestion-text", s));
      const use = makeEl("button", "ghost-btn mini", "Use this goal");
      use.type = "button";
      use.setAttribute("aria-label", "Use this improved goal");
      use.onclick = () => applyGoalSuggestion(s);
      block.appendChild(use);
      panel.appendChild(block);
    });
    appendGoalPanelClose(panel, "Cancel");
  } catch (e) {
    panel.innerHTML = "";
    showPanelError(panel, e.message || "Could not improve the goal.");
    appendGoalPanelClose(panel, "Dismiss");
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function renderTermRow(node, index) {
  const row = document.createElement("div");
  row.className = "term-row";
  row.__node = node;

  row.appendChild(makeSelectCheckbox(node));

  const op = makeOpSelect(node, index, ["AND", "OR", "NOT"]);
  if (op) row.appendChild(op);

  const wrap = document.createElement("div");
  wrap.className = "term-input-wrap";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "term-input";
  input.placeholder = "e.g. mutation";
  input.value = node.text;
  input.oninput = () => {
    queryModel.setText(node, input.value);
    updatePreview();
  };
  wrap.appendChild(input);
  row.appendChild(wrap);
  attachAutocomplete(input, node);

  const suggest = document.createElement("button");
  suggest.type = "button";
  suggest.className = "suggest-btn";
  suggest.textContent = "✨";
  suggest.title = "Suggest related terms (LLM)";
  suggest.onclick = () => openTermSuggestPanel(node, row);
  row.appendChild(suggest);

  const rm = document.createElement("button");
  rm.type = "button";
  rm.className = "remove-term";
  rm.textContent = "×";
  rm.title = "Remove";
  rm.onclick = () => {
    queryModel.removeNode(node);
    ensureAtLeastOneTerm();
    renderQuery();
  };
  row.appendChild(rm);

  return row;
}

function renderGroupBlock(node, index) {
  const block = document.createElement("div");
  block.className = "query-group";
  block.__node = node;

  // Clicking the group's own header/block area cycles selection:
  // none -> group selected -> members selected -> group selected -> ...
  // Interactive controls and the children area are ignored so their own
  // handlers (and nested groups) keep working.
  block.addEventListener("click", (e) => {
    if (e.target.closest("input, select, textarea, button, .node-select, .autocomplete-list, .suggest-panel")) return;
    if (e.target.closest(".group-children")) return;
    cycleGroupSelection(node);
    e.stopPropagation();
  });

  const head = document.createElement("div");
  head.className = "group-head";

  head.appendChild(makeSelectCheckbox(node));

  const op = makeOpSelect(node, index, ["AND", "OR", "NOT"]);
  if (op) head.appendChild(op);

  const label = document.createElement("span");
  label.className = "group-label";
  label.textContent = "( … )";
  head.appendChild(label);

  const addTermBtn = document.createElement("button");
  addTermBtn.type = "button";
  addTermBtn.className = "ghost-btn mini";
  addTermBtn.textContent = "+ term";
  addTermBtn.onclick = () => {
    queryModel.addTerm(node);
    renderQuery();
  };
  head.appendChild(addTermBtn);

  const addGroupBtn = document.createElement("button");
  addGroupBtn.type = "button";
  addGroupBtn.className = "ghost-btn mini";
  addGroupBtn.textContent = "+ group";
  addGroupBtn.onclick = () => {
    queryModel.addGroup(node);
    renderQuery();
  };
  head.appendChild(addGroupBtn);

  const dissolveBtn = document.createElement("button");
  dissolveBtn.type = "button";
  dissolveBtn.className = "ghost-btn mini";
  dissolveBtn.textContent = "dissolve";
  dissolveBtn.title = "Remove this group, keeping its contents";
  dissolveBtn.onclick = () => {
    queryModel.dissolveGroup(node);
    ensureAtLeastOneTerm();
    renderQuery();
  };
  head.appendChild(dissolveBtn);

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "remove-term";
  removeBtn.textContent = "×";
  removeBtn.title = "Remove group and its contents";
  removeBtn.onclick = () => {
    queryModel.removeNode(node);
    ensureAtLeastOneTerm();
    renderQuery();
  };
  head.appendChild(removeBtn);

  block.appendChild(head);

  const children = document.createElement("div");
  children.className = "group-children";
  renderGroupChildren(node, children);
  block.appendChild(children);

  return block;
}

function renderGroupChildren(group, container) {
  group.children.forEach((node, index) => {
    if (node.type === "term") container.appendChild(renderTermRow(node, index));
    else container.appendChild(renderGroupBlock(node, index));
  });
  if (group.children.length === 0 && group !== queryModel.root) {
    const empty = document.createElement("div");
    empty.className = "group-empty";
    empty.textContent = "Empty group — add a term or group.";
    container.appendChild(empty);
  }
}

function renderQuery() {
  pruneSelection();
  const list = document.getElementById("term-list");
  list.innerHTML = "";
  renderGroupChildren(queryModel.root, list);
  syncSelectionUI();
  updatePreview();
}

function updatePreview() {
  const el = document.getElementById("query-preview");
  if (!el) return;
  const q = queryModel.serialize();
  el.textContent = q || "(empty query)";
}

function ensureAtLeastOneTerm() {
  if (queryModel.countTerms() === 0) queryModel.addTerm(queryModel.root);
}

function wrapSelected() {
  const nodes = collectSelectedTopLevel();
  if (!nodes.length) {
    setBanner("Select at least one row or group to wrap.", "error");
    return;
  }
  const group = queryModel.wrapNodes(nodes, "AND");
  if (!group) {
    setBanner("Selected items must share the same parent group.", "error");
    return;
  }
  setBanner("");
  renderQuery();
}

function buildQuery() {
  return queryModel.serialize();
}

/* ---------- search & extract ---------- */

async function doSearch(options) {
  const opts = options || {};
  const merge = !!opts.merge;
  setBanner("");
  const query = buildQuery();
  const sources = [...document.querySelectorAll("#source-list input:checked")].map(c => c.value);
  const maxResults = parseInt(document.getElementById("max-results").value, 10) || 50;
  const years = readYearRange();
  const localFolder = document.getElementById("local-folder").value.trim();
  const localRecursive = document.getElementById("local-recursive").checked;
  const localOnly = !!localFolder && !query && !sources.length;
  if (years.error) { setBanner(years.error, "error"); return; }
  if (localFolder) {
    // With a folder, an empty query + no websites means folder-only. Mixing
    // one without the other is ambiguous, so say exactly what to fix.
    if (query && !sources.length) {
      setBanner("Choose at least one website, or clear the search terms for a folder-only search.", "error");
      return;
    }
    if (!query && sources.length) {
      setBanner("Enter at least one search term, or deselect every website for a folder-only search.", "error");
      return;
    }
  } else {
    if (!query) { setBanner("Enter at least one search term.", "error"); return; }
    if (!sources.length) { setBanner("Choose at least one website.", "error"); return; }
  }

  try {
    const { job_id } = await postJSON("/api/search", {
      query,
      query_tree: queryModel.snapshot(),
      query_selection: snapshotQuerySelection(),
      sources,
      max_results: maxResults,
      excluded_journals: document.getElementById("excluded-journals").value,
      year_from: years.year_from,
      year_to: years.year_to,
      local_folder: localFolder,
      local_recursive: localRecursive,
      local_only: localOnly,
      merge,
    });
    setBusy(true);
    showProgress(true);
    pollJob(job_id);
  } catch (e) {
    setBanner(e.message, "error");
  }
}

const RESERVED_COLUMNS = new Set([
  "paper_key", "row_index", "publication_type", "evidence", "evidence_page",
  "pdf_file", "pdf_source", "doi", "source",
]);

function parseColumnInput(value) {
  const seen = new Map();
  const reserved = [];
  const duplicates = [];
  value.split(",").map(s => s.trim()).filter(Boolean).forEach(name => {
    const key = name.toLowerCase();
    if (RESERVED_COLUMNS.has(key)) {
      if (!reserved.includes(name)) reserved.push(name);
      return;
    }
    if (seen.has(key)) {
      if (!duplicates.includes(name)) duplicates.push(name);
      return;
    }
    seen.set(key, name);
  });
  const errors = [];
  if (reserved.length) {
    errors.push("These column names are reserved and added automatically: " + reserved.join(", "));
  }
  if (duplicates.length) {
    errors.push("Duplicate column names: " + duplicates.join(", "));
  }
  return { columns: [...seen.values()], errors };
}

async function doExtract() {
  setBanner("");
  const goal = document.getElementById("goal").value.trim();
  const parsed = parseColumnInput(document.getElementById("columns").value);
  if (parsed.errors.length) { setBanner(parsed.errors.join(" "), "error"); return; }
  const mode = document.getElementById("extraction-mode").value || "records";
  if (!goal) { setBanner("Describe what data you want to extract.", "error"); return; }

  try {
    const { job_id } = await postJSON("/api/extract", {
      goal,
      columns: parsed.columns,
      mode,
    });
    setBusy(true);
    showProgress(true);
    pollJob(job_id);
  } catch (e) {
    setBanner(e.message, "error");
  }
}

/* ---------- polling ---------- */

function showProgress(show) {
  document.getElementById("progress-card").classList.toggle("hidden", !show);
}

function pollJob(jobId) {
  currentJobId = jobId;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await getJSON("/api/job/" + jobId);
      renderProgress(job);
      if (job.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        currentJobId = null;
        setBusy(false);
        showProgress(false);
        await loadResults();
        await loadRuns();
        if (job.result && job.result.added !== undefined) {
          await refreshHistory("search");
          const excluded = job.result.excluded || 0;
          const suffix = excluded ? ` (${excluded} excluded by journal)` : "";
          const years = formatYearRange(job.result.year_from, job.result.year_to);
          const rangeSuffix = years ? ` (years ${years})` : "";
          const localCount = job.result.local_count || 0;
          const localSuffix = job.result.local_folder
            ? `, ${localCount} local PDF${localCount === 1 ? "" : "s"}`
            : "";
          const run = runsState.runs.find(item => item.id === job.result.run_id);
          const runName = run ? run.name : "";
          const runSuffix = runName ? ` in run \u201c${runName}\u201d` : "";
          if (job.result.merged) {
            const added = job.result.added || 0;
            setBanner(
              `Refresh finished${runSuffix}: ${job.result.total} papers ` +
                `(${added} new${suffix}${rangeSuffix}${localSuffix}); extraction data kept.`,
              "ok"
            );
          } else {
            setBanner(
              `Search finished${runSuffix}: ${job.result.total} papers` +
                `${rangeSuffix}${suffix}${localSuffix}.`,
              "ok"
            );
          }
        } else if (job.result && job.result.extracted !== undefined) {
          await Promise.all([refreshHistory("goal"), refreshHistory("columns")]);
          const modeLabel = job.result.mode === "table" ? "table rows" : "individual records";
          setBanner(`Extraction finished (${modeLabel}): ${job.result.extracted} row(s) extracted, ${job.result.closed} paper(s) closed.`, "ok");
        }
      } else if (job.status === "cancelled") {
        clearInterval(pollTimer);
        pollTimer = null;
        currentJobId = null;
        setBusy(false);
        showProgress(false);
        await loadResults();
        await loadRuns();
        let summary = " Previous results were kept.";
        if (job.result && job.result.extracted !== undefined) {
          await Promise.all([refreshHistory("goal"), refreshHistory("columns")]);
          summary = ` ${job.result.extracted} row(s) extracted, ${job.result.closed} paper(s) closed; completed rows were kept.`;
        } else if (job.result && job.result.added !== undefined) {
          await refreshHistory("search");
        } else if (job.type === "extract") {
          await Promise.all([refreshHistory("goal"), refreshHistory("columns")]);
        } else if (job.type === "search") {
          await refreshHistory("search");
        }
        setBanner("Job cancelled." + summary, "info");
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        pollTimer = null;
        currentJobId = null;
        setBusy(false);
        showProgress(false);
        await loadRuns();
        setBanner("Error: " + (job.error || "something went wrong"), "error");
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
      currentJobId = null;
      setBusy(false);
      setBanner(e.message, "error");
    }
  }, 1200);
}

async function cancelJob() {
  if (!currentJobId) return;
  const jobId = currentJobId;
  const btn = document.getElementById("cancel-job");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Cancelling…";
  }
  try {
    await postJSON("/api/job/" + jobId + "/cancel", {});
  } catch (e) {
    setBanner(e.message, "error");
    try {
      const job = await getJSON("/api/job/" + jobId);
      if (job.status === "running" && !job.cancel_requested) {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Cancel job";
        }
      } else {
        renderProgress(job);
      }
    } catch (ignored) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Cancel job";
      }
    }
  }
}

function renderProgress(job) {
  const p = job.progress || { done: 0, total: 0, current: "" };
  const bar = document.getElementById("progress-bar");
  if (p.total > 0) {
    bar.classList.remove("indeterminate");
    bar.style.width = Math.round((p.done / p.total) * 100) + "%";
  } else {
    bar.classList.add("indeterminate");
    bar.style.width = "40%";
  }
  const status = document.getElementById("progress-status");
  if (p.total > 0) status.textContent = `${p.done} / ${p.total} — ${p.current || ""}`;
  else status.textContent = p.current || "Working…";
  const cancelBtn = document.getElementById("cancel-job");
  if (cancelBtn) {
    const running = job.status === "running";
    cancelBtn.classList.toggle("hidden", !running);
    if (!running) {
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancel job";
      cancelBtn.setAttribute("aria-label", "Cancel the running job");
    } else if (job.cancel_requested) {
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelling…";
      cancelBtn.setAttribute("aria-label", "Cancellation requested");
    } else {
      cancelBtn.disabled = false;
      cancelBtn.textContent = "Cancel job";
      cancelBtn.setAttribute("aria-label", "Cancel the running job");
    }
  }
  const log = document.getElementById("log");
  log.textContent = (job.log || []).join("\n");
  log.scrollTop = log.scrollHeight;
}

/* ---------- results: tabs, grouping, selection ---------- */

let activeResultsTab = "search";
let selectedData = {
  groupKey: "", rowId: "", paper: null, row: null, annotation: null,
};

function cleanValue(value) {
  return value === null || value === undefined ? "" : String(value).trim();
}

function rowObject(header, values) {
  const obj = {};
  header.forEach((name, i) => { obj[name] = values[i] === undefined ? "" : values[i]; });
  return obj;
}

function pmidKey(obj) {
  const pmid = cleanValue(obj.pmid);
  return pmid ? "pmid:" + pmid : "";
}

function doiKey(obj) {
  const doi = cleanValue(obj.doi).toLowerCase();
  return doi ? "doi:" + doi : "";
}

function titleKey(obj) {
  const title = cleanValue(obj.title).replace(/\s+/g, " ").toLowerCase();
  return title ? "title:" + title : "";
}

function canonicalKey(obj) {
  return cleanValue(obj.paper_key) || pmidKey(obj) || doiKey(obj) || titleKey(obj) || "";
}

function searchKeys(obj) {
  const keys = [];
  const add = k => { if (k && !keys.includes(k)) keys.push(k); };
  add(cleanValue(obj.paper_key));
  add(pmidKey(obj));
  add(doiKey(obj));
  add(titleKey(obj));
  return keys;
}

// Extracted rows have no title column of their own. Only real identities may
// link them: stored paper_key, PMID or DOI. Everything else is "unlinked".
function extractedKeys(obj) {
  const keys = [];
  const add = k => { if (k && !keys.includes(k)) keys.push(k); };
  add(cleanValue(obj.paper_key));
  add(pmidKey(obj));
  add(doiKey(obj));
  return keys;
}

function buildSearchIndex(sheet) {
  const index = new Map();
  if (!sheet) return index;
  const header = sheet.header || [];
  (sheet.rows || []).forEach(values => {
    const obj = rowObject(header, values);
    const key = canonicalKey(obj);
    searchKeys(obj).forEach(candidate => {
      if (!index.has(candidate)) index.set(candidate, { obj, key: key || candidate });
    });
  });
  return index;
}

function findSearchMatch(obj, index) {
  for (const key of extractedKeys(obj)) {
    if (index.has(key)) return index.get(key);
  }
  return null;
}

function columnLabel(name) {
  if (name === "row_index") return "#";
  if (name === "publication_type") return "type";
  return name;
}

function publicationType(obj) {
  return cleanValue(obj && obj.publication_type).toLowerCase();
}

// A paper group is a preprint when its search record or any of its extracted
// rows says so (old extracted rows may predate the search-row backfill).
function isPreprintGroup(group) {
  if (!group) return false;
  if (publicationType(group.paper) === "preprint") return true;
  return (group.rows || []).some(row => publicationType(row.obj) === "preprint");
}

function setResultsTab(name, focus) {
  const tabs = [...document.querySelectorAll(".tab-btn")];
  const available = tabs.some(t => t.dataset.tab === name && !t.hidden);
  if (!available) name = "search";
  tabs.forEach(btn => {
    const active = btn.dataset.tab === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
    btn.tabIndex = active ? 0 : -1;
    if (active && focus) btn.focus();
  });
  document.getElementById("tab-search").classList.toggle("hidden", name !== "search");
  document.getElementById("tab-data").classList.toggle("hidden", name !== "data");
  activeResultsTab = name;
}

function setupResultsTabs() {
  const tablist = document.querySelector(".tabs");
  if (!tablist) return;
  tablist.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab-btn");
    if (!btn || btn.hidden) return;
    setResultsTab(btn.dataset.tab, false);
  });
  tablist.addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = [...tablist.querySelectorAll(".tab-btn:not([hidden])")];
    if (!tabs.length) return;
    let idx = tabs.findIndex(t => t.dataset.tab === activeResultsTab);
    if (idx === -1) idx = 0;
    if (e.key === "ArrowRight") idx = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft") idx = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") idx = 0;
    else if (e.key === "End") idx = tabs.length - 1;
    e.preventDefault();
    setResultsTab(tabs[idx].dataset.tab, true);
  });
}

async function loadResults() {
  let data;
  try {
    data = await getJSON("/api/results");
  } catch (e) {
    return;
  }
  const card = document.getElementById("results-card");
  const figureItems = Array.isArray(data.figures) ? data.figures : [];
  const hasAny = data.search || data.extracted || data.closed || figureItems.length > 0;
  card.classList.toggle("hidden", !hasAny);

  if (data.search) {
    document.getElementById("search-count").textContent = `(${data.search.rows.length})`;
    renderTable("search-table", data.search.header, data.search.rows, {
      hidden: ["paper_key"],
    });
  } else {
    document.getElementById("search-count").textContent = "";
    document.getElementById("search-table").innerHTML = "";
  }

  const extracted = data.extracted;
  const hasExtracted = !!(extracted && extracted.rows && extracted.rows.length);
  const hasFigures = figureItems.length > 0;
  const dataTab = document.getElementById("tab-btn-data");
  dataTab.hidden = !(hasExtracted || hasFigures);
  document.getElementById("data-tab-count").textContent = hasExtracted
    ? `(${extracted.rows.length})`
    : (hasFigures ? `(${figureItems.length} figures)` : "");

  if (hasExtracted) {
    renderDataGroups(
      extracted,
      buildSearchIndex(data.search),
      data.annotations || {},
    );
  } else {
    document.getElementById("extracted-count").textContent = "";
    document.getElementById("paper-group-count").textContent = "";
    const groups = document.getElementById("data-groups");
    groups.innerHTML = "";
    if (hasFigures) {
      groups.appendChild(makeEl("p", "empty-note", "No extracted rows yet — figures only."));
    }
    clearSelectedData();
    if (!hasFigures && activeResultsTab === "data") setResultsTab("search", false);
  }

  renderFigures(figureItems);
}

function renderTable(containerId, header, rows, options) {
  const hidden = new Set((options && options.hidden) || []);
  const columns = header
    .map((name, index) => ({ name, index }))
    .filter(col => !hidden.has(col.name));
  const container = document.getElementById(containerId);
  container.innerHTML = "";

  if (!rows.length) {
    container.appendChild(makeEl("p", "empty-note", "No rows yet."));
    return;
  }

  const table = document.createElement("table");

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  columns.forEach(col => {
    const th = document.createElement("th");
    th.textContent = columnLabel(col.name);
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const cap = 300;
  rows.slice(0, cap).forEach(r => {
    const tr = document.createElement("tr");
    columns.forEach(col => {
      const td = document.createElement("td");
      let v = r[col.index];
      if (v === null || v === undefined) v = "";
      v = String(v);
      if (col.name === "is_open_access") {
        td.className = v === "yes" ? "oa-yes" : "oa-no";
        v = v === "yes" ? "open" : (v === "no" ? "closed" : "");
      }
      if (col.name === "publication_type") {
        td.className = "pub-type";
        if (v.trim().toLowerCase() === "preprint") {
          td.classList.add("pub-type-preprint");
        }
      }
      td.textContent = v;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.appendChild(table);

  if (rows.length > cap) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = `Showing first ${cap} of ${rows.length} rows — download the Excel for everything.`;
    container.appendChild(note);
  }
}

function groupExtractedRows(extracted, searchIndex, annotations) {
  const header = extracted.header || [];
  const groups = new Map();
  const unlinkedKey = "__unlinked__";
  const annotationMap = annotations || {};

  (extracted.rows || []).forEach((values, rawIndex) => {
    const obj = rowObject(header, values);
    const match = findSearchMatch(obj, searchIndex);
    const keys = extractedKeys(obj);
    const key = match ? (match.key || keys[0]) : (keys[0] || "");
    const groupKey = key || unlinkedKey;

    if (!groups.has(groupKey)) {
      groups.set(groupKey, {
        key: groupKey,
        unlinked: groupKey === unlinkedKey,
        paper: match ? match.obj : null,
        annotation: annotationMap[groupKey] || annotationMap[keys[0]] || null,
        rows: [],
      });
    }
    groups.get(groupKey).rows.push({
      obj,
      rawIndex,
      displayRow: cleanValue(obj.row_index) || String(rawIndex + 2),
    });
  });

  return groups;
}

function groupTitle(group) {
  if (group.unlinked) return "Unlinked rows";
  if (group.paper) return cleanValue(group.paper.title) || "Untitled paper";
  if (group.key.startsWith("title:")) return group.key.slice(6);
  return "Paper not found in the current search results";
}

function renderDataGroups(extracted, searchIndex, annotations) {
  const groups = groupExtractedRows(extracted, searchIndex, annotations);
  const container = document.getElementById("data-groups");
  container.innerHTML = "";

  const totalRows = (extracted.rows || []).length;
  document.getElementById("extracted-count").textContent =
    `${totalRows} extracted row${totalRows === 1 ? "" : "s"}`;
  document.getElementById("paper-group-count").textContent =
    `${groups.size} paper group${groups.size === 1 ? "" : "s"}`;

  if (!groups.size) {
    container.appendChild(makeEl("p", "empty-note", "No extracted rows yet."));
    clearSelectedData();
    return;
  }

  const header = (extracted.header || []).filter(name => name !== "paper_key");
  const cap = 300;
  let shown = 0;
  let selectedStillVisible = false;

  groups.forEach(group => {
    if (shown >= cap) return;
    const section = document.createElement("section");
    section.className = "data-group" + (group.unlinked ? " unlinked" : "");

    const head = document.createElement("div");
    head.className = "data-group-head";
    const heading = document.createElement("div");
    heading.className = "data-group-heading";

    const titleRow = makeEl("div", "data-group-title-row");
    titleRow.appendChild(makeEl("h4", "data-group-title", groupTitle(group)));
    if (isPreprintGroup(group)) {
      const preprintBadge = makeEl("span", "data-group-badge data-group-badge-preprint", "preprint");
      preprintBadge.title = "Preprint (not a peer-reviewed journal article)";
      titleRow.appendChild(preprintBadge);
    }
    heading.appendChild(titleRow);

    if (group.paper) {
      const meta = [
        cleanValue(group.paper.journal),
        cleanValue(group.paper.year),
        cleanValue(group.paper.source),
      ].filter(Boolean).join(" · ");
      if (meta) heading.appendChild(makeEl("p", "data-group-meta", meta));
    }

    const keyText = group.unlinked ? "" : group.key;
    if (keyText) heading.appendChild(makeEl("code", "data-group-key", keyText));
    head.appendChild(heading);
    head.appendChild(makeEl(
      "span",
      "data-group-count",
      `${group.rows.length} row${group.rows.length === 1 ? "" : "s"}`
    ));
    section.appendChild(head);

    const wrap = document.createElement("div");
    wrap.className = "table-wrap data-table-wrap";
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    header.forEach(name => {
      const th = document.createElement("th");
      th.textContent = columnLabel(name);
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    group.rows.slice(0, Math.max(0, cap - shown)).forEach(rowState => {
      const tr = document.createElement("tr");
      tr.className = "data-row";
      const rowId = group.key + "::" + rowState.rawIndex;
      tr.dataset.rowId = rowId;
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      tr.setAttribute("aria-label", `Select row ${rowState.displayRow} from ${groupTitle(group)}`);
      if (selectedData.rowId === rowId) {
        tr.classList.add("selected");
        selectedStillVisible = true;
        selectedData.annotation = group.annotation || selectedData.annotation;
      }

      header.forEach(name => {
        const td = document.createElement("td");
        td.textContent = cleanValue(rowState.obj[name]);
        tr.appendChild(td);
      });
      tr.addEventListener("click", () => selectDataRow(group, rowState, tr));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectDataRow(group, rowState, tr);
        }
      });
      tbody.appendChild(tr);
      shown += 1;
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    section.appendChild(wrap);
    container.appendChild(section);
  });

  if (totalRows > cap) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = `Showing first ${cap} of ${totalRows} rows — download the Excel for everything.`;
    container.appendChild(note);
  }

  if (selectedData.rowId && !selectedStillVisible) clearSelectedData();
  else if (selectedData.rowId) renderSelectionDetails();
}

/* ---------- figure gallery / lightbox ---------- */

let figureModalPreviousFocus = null;

function figureImageUrl(id) {
  return "/api/figure/" + encodeURIComponent(String(id || ""));
}

function shortCaption(text, limit) {
  const value = cleanValue(text);
  if (!value) return "";
  const cap = limit || 180;
  return value.length > cap ? value.slice(0, cap - 1).trimEnd() + "…" : value;
}

function figureKindLabel(kind) {
  if (kind === "image") return "embedded image";
  if (kind === "page") return "full page (caption only)";
  return "caption crop";
}

function renderFigures(figures) {
  const section = document.getElementById("figures-section");
  const gallery = document.getElementById("figures-gallery");
  const count = document.getElementById("figures-count");
  const actionButton = document.getElementById("dl-figures");
  if (!section || !gallery || !count) return;

  const items = (Array.isArray(figures) ? figures : [])
    .filter(fig => fig && fig.id);
  const hasFigures = items.length > 0;
  section.classList.toggle("hidden", !hasFigures);
  if (actionButton) actionButton.classList.toggle("hidden", !hasFigures);
  count.textContent = hasFigures ? `(${items.length})` : "";
  gallery.innerHTML = "";
  if (!hasFigures) {
    closeFigureModal();
    return;
  }

  // Group thumbnails by paper, preserving the API's sorted order.
  const groups = new Map();
  items.forEach(fig => {
    const key = cleanValue(fig.paper_key) || "__paper__";
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        title: cleanValue(fig.title) || (key === "__paper__" ? "Unknown paper" : key),
        figures: [],
      });
    }
    groups.get(key).figures.push(fig);
  });

  groups.forEach(group => {
    const groupEl = document.createElement("section");
    groupEl.className = "figure-group";

    const head = document.createElement("div");
    head.className = "figure-group-head";
    head.appendChild(makeEl("h4", "figure-group-title", group.title));
    head.appendChild(makeEl(
      "span",
      "figure-group-count",
      `${group.figures.length} figure${group.figures.length === 1 ? "" : "s"}`
    ));
    if (group.key !== "__paper__") {
      head.appendChild(makeEl("code", "figure-group-key", group.key));
    }
    groupEl.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "figure-grid";
    group.figures.forEach(fig => {
      const label = cleanValue(fig.label) || "Figure";
      const page = cleanValue(fig.page);
      const caption = shortCaption(fig.caption, 180);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "figure-thumb";
      button.setAttribute(
        "aria-label",
        `Open ${label}${page ? " (page " + page + ")" : ""} from ${group.title}`
      );

      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = figureImageUrl(fig.id);
      img.alt = `${label}${page ? ", page " + page : ""} — ${group.title}`;
      img.addEventListener("error", () => button.classList.add("broken"));

      const body = document.createElement("div");
      body.className = "figure-thumb-body";
      body.appendChild(makeEl("p", "figure-thumb-label", label));
      body.appendChild(makeEl(
        "p",
        "figure-thumb-meta",
        [page ? `Page ${page}` : "", figureKindLabel(fig.kind)].filter(Boolean).join(" · ")
      ));
      if (caption) {
        const captionEl = makeEl("p", "figure-thumb-caption", caption);
        captionEl.title = cleanValue(fig.caption);
        body.appendChild(captionEl);
      }

      button.appendChild(img);
      button.appendChild(body);
      button.addEventListener("click", () => openFigureModal(fig, group.title));
      grid.appendChild(button);
    });

    groupEl.appendChild(grid);
    gallery.appendChild(groupEl);
  });
}

function openFigureModal(fig, paperTitle) {
  const modal = document.getElementById("figure-modal");
  const image = document.getElementById("figure-modal-image");
  if (!modal || !image || !fig) return;
  const url = figureImageUrl(fig.id);
  const label = cleanValue(fig.label) || "Figure";
  const page = cleanValue(fig.page);

  figureModalPreviousFocus = document.activeElement;
  image.src = url;
  image.alt = `${label}${page ? ", page " + page : ""} — ${paperTitle || "paper"}`;

  document.getElementById("figure-modal-label").textContent = label;
  document.getElementById("figure-modal-title").textContent = paperTitle || "";
  document.getElementById("figure-modal-meta").textContent = [
    page ? `Page ${page}` : "",
    figureKindLabel(fig.kind),
  ].filter(Boolean).join(" · ");
  document.getElementById("figure-modal-caption").textContent =
    cleanValue(fig.caption) || "No caption text was found for this figure.";
  const reasonEl = document.getElementById("figure-modal-reason");
  const reason = cleanValue(fig.reason);
  if (reasonEl) {
    reasonEl.textContent = reason ? `Selected because: ${reason}` : "";
    reasonEl.hidden = !reason;
  }

  const link = document.getElementById("figure-modal-link");
  if (link) {
    link.href = url;
    link.hidden = false;
  }
  modal.classList.remove("hidden");
  document.body.classList.add("figure-modal-open");
  const close = document.getElementById("figure-modal-close");
  if (close) close.focus({ preventScroll: true });
}

function closeFigureModal() {
  const modal = document.getElementById("figure-modal");
  if (!modal || modal.classList.contains("hidden")) return;
  const image = document.getElementById("figure-modal-image");
  modal.classList.add("hidden");
  document.body.classList.remove("figure-modal-open");
  if (image) {
    image.removeAttribute("src");
    image.alt = "";
  }
  if (figureModalPreviousFocus && typeof figureModalPreviousFocus.focus === "function") {
    figureModalPreviousFocus.focus({ preventScroll: true });
  }
  figureModalPreviousFocus = null;
}

function setupFigureModal() {
  const modal = document.getElementById("figure-modal");
  if (!modal) return;
  document.getElementById("figure-modal-close")?.addEventListener("click", closeFigureModal);
  modal.querySelectorAll("[data-close-figure]").forEach(el => {
    el.addEventListener("click", closeFigureModal);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) {
      e.preventDefault();
      closeFigureModal();
    }
  });
}

function evidenceMetadataForRow(annotation, row) {
  if (!annotation || !Array.isArray(annotation.evidence) || !row) return null;
  const rowIndex = cleanValue(row.row_index);
  if (rowIndex) {
    const byIndex = annotation.evidence.find(
      item => item && cleanValue(item.row_index) === rowIndex
    );
    if (byIndex) return byIndex;
  }
  const quote = cleanValue(row.evidence);
  if (quote) {
    return annotation.evidence.find(
      item => item && cleanValue(item.text) === quote
    ) || null;
  }
  return null;
}

function evidenceStatusLabel(status) {
  if (status === "exact") return "exact match";
  if (status === "fuzzy") return "fuzzy match";
  if (status === "unresolved") return "not located in the PDF";
  return "";
}

function resetPdfViewer() {
  const viewer = document.getElementById("pdf-viewer");
  const link = document.getElementById("pdf-new-tab");
  const unavailable = document.getElementById("pdf-unavailable");
  if (viewer) {
    viewer.classList.add("hidden");
    viewer.removeAttribute("src");
    delete viewer.dataset.currentUrl;
    viewer.src = "about:blank";
  }
  if (link) {
    link.classList.add("hidden");
    link.hidden = true;
    link.removeAttribute("href");
  }
  if (unavailable) unavailable.classList.add("hidden");
}

function selectDataRow(group, rowState, tr) {
  document.querySelectorAll(".data-row.selected").forEach(el => el.classList.remove("selected"));
  tr.classList.add("selected");
  selectedData = {
    groupKey: group.key,
    rowId: group.key + "::" + rowState.rawIndex,
    paper: group.paper,
    row: rowState.obj,
    rowNumber: rowState.displayRow,
    groupTitle: groupTitle(group),
    annotation: group.annotation || null,
  };
  renderSelectionDetails();
}

function clearSelectedData() {
  selectedData = {
    groupKey: "", rowId: "", paper: null, row: null, annotation: null,
  };
  document.querySelectorAll(".data-row.selected").forEach(el => el.classList.remove("selected"));
  const placeholder = document.getElementById("pdf-placeholder");
  const details = document.getElementById("selection-details");
  const summary = document.getElementById("selection-summary");
  const evidenceText = document.getElementById("evidence-text");
  const evidenceMeta = document.getElementById("evidence-meta");
  if (placeholder) placeholder.classList.remove("hidden");
  if (details) details.classList.add("hidden");
  if (summary) summary.innerHTML = "";
  if (evidenceText) evidenceText.textContent = "";
  if (evidenceMeta) evidenceMeta.textContent = "";
  resetPdfViewer();
}

function renderSelectionDetails() {
  const placeholder = document.getElementById("pdf-placeholder");
  const details = document.getElementById("selection-details");
  const summary = document.getElementById("selection-summary");
  const evidenceTextEl = document.getElementById("evidence-text");
  const evidenceMetaEl = document.getElementById("evidence-meta");
  if (!placeholder || !details) return;
  if (!selectedData.rowId) { clearSelectedData(); return; }
  if (!summary || !evidenceTextEl || !evidenceMetaEl) return;

  placeholder.classList.add("hidden");
  details.classList.remove("hidden");
  summary.innerHTML = "";

  const paper = selectedData.paper || {};
  const row = selectedData.row || {};
  const annotation = selectedData.annotation || null;
  const evidenceMetaForRow = evidenceMetadataForRow(annotation, row);

  summary.appendChild(makeEl("p", "selection-eyebrow", "Selected paper"));
  summary.appendChild(makeEl(
    "h3",
    "selection-title",
    selectedData.groupTitle || "Selected paper"
  ));

  const meta = [
    cleanValue(paper.journal),
    cleanValue(paper.year),
    cleanValue(paper.source),
  ].filter(Boolean).join(" · ");
  if (meta) summary.appendChild(makeEl("p", "selection-meta", meta));

  const facts = document.createElement("dl");
  facts.className = "selection-facts";
  const addFact = (label, value) => {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    facts.appendChild(dt);
    facts.appendChild(dd);
  };
  addFact("Paper key", selectedData.groupKey === "__unlinked__" ? "unlinked" : selectedData.groupKey);
  addFact("Row", selectedData.rowNumber);
  addFact("DOI", cleanValue(row.doi));
  if (annotation && cleanValue(annotation.pdf_source)) {
    addFact("PDF source", cleanValue(annotation.pdf_source));
  }
  summary.appendChild(facts);

  // The quote panel stays useful even when the browser's PDF viewer ignores
  // embedded highlights (or when no PDF was archived at all).
  const quote = cleanValue(row.evidence)
    || cleanValue(evidenceMetaForRow && evidenceMetaForRow.text);
  evidenceTextEl.textContent = quote || "No evidence quote was extracted for this row.";
  evidenceTextEl.classList.toggle("muted", !quote);

  const page = cleanValue(evidenceMetaForRow && evidenceMetaForRow.page)
    || cleanValue(row.evidence_page);
  const statusLabel = evidenceStatusLabel(evidenceMetaForRow && evidenceMetaForRow.status);
  const detailsBits = [];
  if (page) detailsBits.push("Page " + page);
  if (statusLabel) detailsBits.push(statusLabel);
  if (evidenceMetaForRow) {
    evidenceMetaEl.dataset.status = cleanValue(evidenceMetaForRow.status);
  } else {
    delete evidenceMetaEl.dataset.status;
  }
  evidenceMetaEl.textContent = detailsBits.join(" · ");

  const viewer = document.getElementById("pdf-viewer");
  const link = document.getElementById("pdf-new-tab");
  const unavailable = document.getElementById("pdf-unavailable");
  const groupKey = selectedData.groupKey;
  const hasPdf = !!(
    annotation
    && annotation.available
    && groupKey
    && groupKey !== "__unlinked__"
  );

  if (hasPdf && viewer && link && unavailable) {
    const pageNumber = page || "1";
    const url = "/api/annotated-pdf/"
      + encodeURIComponent(groupKey)
      + "#page=" + encodeURIComponent(pageNumber);
    if (viewer.dataset.currentUrl !== url) {
      viewer.dataset.currentUrl = url;
      viewer.src = url;
    }
    viewer.classList.remove("hidden");
    link.href = url;
    link.hidden = false;
    link.classList.remove("hidden");
    unavailable.classList.add("hidden");
  } else if (viewer && link && unavailable) {
    viewer.classList.add("hidden");
    viewer.removeAttribute("src");
    delete viewer.dataset.currentUrl;
    viewer.src = "about:blank";
    link.classList.add("hidden");
    link.hidden = true;
    link.removeAttribute("href");
    unavailable.classList.remove("hidden");
  }
}

function downloadFile(name) {
  window.location.href = "/download/" + name;
}

async function openOutputFolder() {
  try {
    await postJSON("/open-output", {});
  } catch (e) {
    setBanner(e.message, "error");
  }
}

/* ---------- numeric-only inputs ---------- */
function bindNumericInputs() {
  document.querySelectorAll("input[data-numeric]").forEach(el => {
    el.addEventListener("input", () => {
      const digits = el.value.replace(/\D/g, "");
      if (el.value !== digits) el.value = digits;
    });
    el.addEventListener("keydown", (e) => {
      if (e.key.length === 1 && !/[0-9]/.test(e.key) && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
      }
    });
  });
}

/* ---------- init ---------- */

document.getElementById("add-term").onclick = () => {
  queryModel.addTerm(queryModel.root);
  renderQuery();
};

document.getElementById("add-group").onclick = () => {
  queryModel.addGroup(queryModel.root);
  renderQuery();
};

document.getElementById("wrap-selected").onclick = wrapSelected;

document.getElementById("suggest-all").onclick = suggestAllTerms;

bindHistorySelect("search", applySearchHistory);
bindHistorySelect("goal", entry => applyTextHistory(entry, "goal"));
bindHistorySelect("columns", entry => applyTextHistory(entry, "columns"));
HISTORY_KINDS.forEach(bindHistoryClear);

window.addEventListener("DOMContentLoaded", async () => {
  bindNumericInputs();
  setupResultsTabs();
  setupFigureModal();
  const runSelect = document.getElementById("run-select");
  if (runSelect) runSelect.addEventListener("change", updateRunButtons);
  queryModel.addTerm(queryModel.root);
  queryModel.addTerm(queryModel.root);
  renderQuery();
  try {
    await Promise.all([loadSources(), loadSettings()]);
  } catch (e) { /* non-fatal */ }
  await loadRuns({ applyActive: true });
  await loadHistory();
  await loadResults();
  await checkStatus();
});

"use strict";

let pollTimer = null;

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

async function loadSettings() {
  const s = await getJSON("/api/settings");
  document.getElementById("set-email").value = s.email || "";
  document.getElementById("set-model").value = s.model || "";
  document.getElementById("set-max").value = s.max_results || 50;
  document.getElementById("max-results").value = s.max_results || 50;
}

async function saveSettings() {
  try {
    const body = {
      email: document.getElementById("set-email").value.trim(),
      model: document.getElementById("set-model").value.trim(),
      max_results: parseInt(document.getElementById("set-max").value, 10) || 50,
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

/* ---------- query builder (nested boolean groups) ---------- */

/*
 * Tree model
 * ----------
 * Nodes are either:
 *   { id: string, type: "term",  text: string, op: "AND"|"OR"|"NOT" }
 *   { id: string, type: "group", op: "AND"|"OR", children: Node[] }
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

  return {
    root,
    findParent,
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
    cb.checked = selectedIds.has(node.id);
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
  cb.checked = selectedIds.has(node.id);
  cb.__node = node;
  cb.onchange = () => {
    if (cb.checked) {
      selectedIds.add(node.id);
      childrenSelectedGroups.delete(node.id);
    } else {
      selectedIds.delete(node.id);
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

  const op = makeOpSelect(node, index, ["AND", "OR"]);
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

async function doSearch() {
  setBanner("");
  const query = buildQuery();
  const sources = [...document.querySelectorAll("#source-list input:checked")].map(c => c.value);
  const maxResults = parseInt(document.getElementById("max-results").value, 10) || 50;
  if (!query) { setBanner("Enter at least one search term.", "error"); return; }
  if (!sources.length) { setBanner("Choose at least one website.", "error"); return; }

  try {
    const { job_id } = await postJSON("/api/search", { query, sources, max_results: maxResults });
    setBusy(true);
    showProgress(true);
    pollJob(job_id);
  } catch (e) {
    setBanner(e.message, "error");
  }
}

async function doExtract() {
  setBanner("");
  const goal = document.getElementById("goal").value.trim();
  const columns = document.getElementById("columns").value
    .split(",").map(s => s.trim()).filter(Boolean);
  if (!goal) { setBanner("Describe what data you want to extract.", "error"); return; }

  try {
    const { job_id } = await postJSON("/api/extract", { goal, columns });
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
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await getJSON("/api/job/" + jobId);
      renderProgress(job);
      if (job.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        setBusy(false);
        showProgress(false);
        await loadResults();
        if (job.result && job.result.added !== undefined) {
          setBanner(`Search finished: ${job.result.total} papers.`, "ok");
        } else if (job.result && job.result.extracted !== undefined) {
          setBanner(`Extraction finished: ${job.result.extracted} row(s) extracted, ${job.result.closed} paper(s) closed.`, "ok");
        }
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        pollTimer = null;
        setBusy(false);
        showProgress(false);
        setBanner("Error: " + (job.error || "something went wrong"), "error");
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
      setBusy(false);
      setBanner(e.message, "error");
    }
  }, 1200);
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
  const log = document.getElementById("log");
  log.textContent = (job.log || []).join("\n");
  log.scrollTop = log.scrollHeight;
}

/* ---------- results ---------- */

async function loadResults() {
  let data;
  try {
    data = await getJSON("/api/results");
  } catch (e) {
    return;
  }
  const card = document.getElementById("results-card");
  const hasAny = data.search || data.extracted || data.closed;
  card.classList.toggle("hidden", !hasAny);

  if (data.search) {
    document.getElementById("search-count").textContent = `(${data.search.rows.length})`;
    renderTable("search-table", data.search.header, data.search.rows);
  }
  if (data.extracted) {
    document.getElementById("extracted-count").textContent = `(${data.extracted.rows.length})`;
    renderTable("extracted-table", data.extracted.header, data.extracted.rows);
  }
}

function renderTable(containerId, header, rows) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  const table = document.createElement("table");

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  header.forEach(h => {
    const th = document.createElement("th");
    th.textContent = h;
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const cap = 300;
  rows.slice(0, cap).forEach(r => {
    const tr = document.createElement("tr");
    header.forEach((h, i) => {
      const td = document.createElement("td");
      let v = r[i];
      if (v === null || v === undefined) v = "";
      v = String(v);
      if (h === "is_open_access") {
        td.className = v === "yes" ? "oa-yes" : "oa-no";
        v = v === "yes" ? "open" : (v === "no" ? "closed" : "");
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

window.addEventListener("DOMContentLoaded", async () => {
  bindNumericInputs();
  queryModel.addTerm(queryModel.root);
  queryModel.addTerm(queryModel.root);
  renderQuery();
  try {
    await Promise.all([loadSources(), loadSettings()]);
  } catch (e) { /* non-fatal */ }
  await loadResults();
  await checkStatus();
});

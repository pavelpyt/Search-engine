// Research Agents UI: renders the pipeline, live log, answer and per-agent inspector
// from the SSE event stream produced by app/orchestrator.py.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const json = (o) => JSON.stringify(o, null, 2);
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : "#");
const usd = (n) => (n == null ? "" : n < 0.01 ? "< 0,01 USD" : `${n.toFixed(2).replace(".", ",")} USD`);
const TAGCLS = { idle: "", working: "hl", done: "good", returned: "warn", error: "bad" };

const STATUS_CZ = { idle: "ČEKÁ", working: "BĚŽÍ", done: "HOTOVO", returned: "VRÁCENO", error: "CHYBA" };
const EXAMPLES = [
  "Jaký je stav zavádění AI ve středních firmách v ČR a co jim nejvíc brání?",
  "What does the EU AI Act require from companies using chatbots in customer support?",
  "Vyplatí se v roce 2026 tepelné čerpadlo do rodinného domu v Česku?",
];

let CONFIG = null;
let state = null;
let source = null;

function freshState() {
  const agents = {};
  for (const a of CONFIG.agents) agents[a.key] = { status: "idle", step: "", model: null, usage: null,
    invocations: [], calls: [], tools: [], skills: new Map(), handoffsIn: [], skillResult: null };
  return { agents, edges: {}, runId: null, finished: false, validations: [], errors: 0 };
}

// ---------- pipeline cards ----------
function renderPipeline() {
  const root = $("#pipeline");
  root.innerHTML = "";
  CONFIG.agents.forEach((a, i) => {
    if (i > 0) {
      const arrow = document.createElement("div");
      arrow.className = "arrow";
      arrow.id = `edge-${CONFIG.agents[i - 1].key}-${a.key}`;
      arrow.innerHTML = "→";
      root.appendChild(arrow);
    }
    const card = document.createElement("article");
    card.className = "card idle";
    card.dataset.agent = a.key;
    card.tabIndex = 0;
    card.innerHTML = `
      <div class="card-head"><span class="name">${esc(a.label)}</span><span class="no">#0${i + 1}</span></div>
      <div class="role">${esc(a.role)}</div>
      <div><span class="tag status">${STATUS_CZ.idle}</span></div>
      <div class="step"></div>
      <div class="meta">${a.skills.map((s) => `<span class="tag" title="Skill">SKILL · ${esc(s)}</span>`).join("")}
        <span class="tag model hidden"></span><span class="tag tokens hidden"></span></div>`;
    card.addEventListener("click", () => openInspector(a.key));
    card.addEventListener("keydown", (e) => e.key === "Enter" && openInspector(a.key));
    root.appendChild(card);
  });
}

function updateCard(key) {
  const a = state.agents[key];
  const card = $(`.card[data-agent="${key}"]`);
  card.className = `card ${a.status}`;
  const st = $(".status", card);
  st.textContent = STATUS_CZ[a.status] || a.status;
  st.className = `tag status ${TAGCLS[a.status] || ""}`;
  $(".step", card).textContent = a.step || "";
  const m = $(".tag.model", card), t = $(".tag.tokens", card);
  if (a.model) { m.textContent = a.model.replace("claude-", ""); m.classList.remove("hidden"); }
  if (a.usage) { t.textContent = `${(a.usage.input_tokens + a.usage.output_tokens).toLocaleString("cs-CZ")} TOK · ${usd(a.usage.cost_usd)}`; t.classList.remove("hidden"); }
}

function setEdge(from, to, cls, label) {
  const el = $(`#edge-${from}-${to}`) || $(`#edge-${to}-${from}`);
  if (!el) return;
  el.className = `arrow ${cls}`;
  el.innerHTML = `${cls === "returned" ? "↩" : "→"}<span>${esc(label || "")}</span>`;
}

// ---------- log ----------
function log(icon, html, cls = "") {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  const t = new Date().toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  li.innerHTML = `<span>${icon}</span><div>${html} <span class="t">${t}</span></div>`;
  const ol = $("#log");
  ol.appendChild(li);
  ol.scrollTop = ol.scrollHeight;
}
const label = (k) => CONFIG.agents.find((a) => a.key === k)?.label || k;
const code = (k) => ["", "RTR", "RSR", "ANL", "WRT"][CONFIG.agents.findIndex((a) => a.key === k) + 1] || "AGT";
const short = (o, n = 120) => { const s = typeof o === "string" ? o : JSON.stringify(o); return s.length > n ? s.slice(0, n) + "…" : s; };

// ---------- events ----------
const STATUS_LABEL = { COMPLETED: "HOTOVO", PARTIAL: "ČÁSTEČNĚ", INSUFFICIENT_EVIDENCE: "MÁLO DŮKAZŮ", FAILED: "CHYBA" };
const STATUS_TAG = { COMPLETED: "good", PARTIAL: "warn", INSUFFICIENT_EVIDENCE: "warn", FAILED: "bad" };
const VSTATUS_TAG = { SUPPORTED: "good", PARTIALLY_SUPPORTED: "warn", CONTRADICTED: "bad", UNSUPPORTED: "bad", NOT_CHECKED: "" };
const VSTATUS_CZ = { SUPPORTED: "PODLOŽENO", PARTIALLY_SUPPORTED: "ČÁSTEČNĚ", CONTRADICTED: "V ROZPORU", UNSUPPORTED: "NEPODLOŽENO", NOT_CHECKED: "NEOVĚŘENO" };

function onEvent(e) {
  const a = e.agent && state.agents[e.agent];
  switch (e.type) {
    case "run_started":
      state.runId = e.run_id;
      $("#logLink").href = `/api/runs/${e.run_id}`;
      $("#logLink").classList.remove("hidden");
      log("RUN", `Start: <b>${esc(e.question)}</b>`);
      break;
    case "agent_status":
      a.status = e.status; a.step = e.step; updateCard(e.agent);
      if (e.status !== "done" || e.step) log(code(e.agent), `<b>${label(e.agent)}</b> · ${esc(STATUS_CZ[e.status])}${e.step ? ": " + esc(e.step) : ""}`,
        e.status === "returned" ? "warn" : "");
      break;
    case "skill_loaded":
      a.skills.set(e.skill, e);
      log("SKL", `${label(e.agent)} načetl skill <code>${esc(e.skill)}</code> v${esc(e.version)} do system promptu`);
      break;
    case "skill_result":
      a.skillResult = e;
      log("SKL", `Skill <code>${esc(e.skill)}</code> ${e.executed ? "vykonán" : "NEBYL vykonán"}: <code>${esc(e.tool)}</code> ${e.invocations}× · pokrytí ${e.sources_total - e.not_scored.length}/${e.sources_total} zdrojů`, e.executed && !e.not_scored.length ? "" : "warn");
      break;
    case "agent_request":
      a.model = e.model; updateCard(e.agent);
      a.invocations.push({ model: e.model, system: e.system, tools: e.tools, messages: e.messages, final: null, output: null });
      log("API", `${label(e.agent)} → <code>${esc(e.model)}</code>, ${e.tools.length} tools`);
      break;
    case "api_request":
      a.calls.push({ request: e, response: null });
      break;
    case "api_response": {
      const c = a.calls.find((x) => x.request.call_id === e.call_id && !x.response);
      if (c) c.response = e;
      break;
    }
    case "agent_messages":
      if (a.invocations.at(-1)) a.invocations.at(-1).final = e.messages;
      break;
    case "tool_call":
      a.tools.push({ ...e, result: null });
      log(e.kind === "server" ? "WEB" : "TLS", `${label(e.agent)} volá <code>${esc(e.name)}</code> ${esc(short(e.input, 90))}`);
      break;
    case "tool_result": {
      const call = [...a.tools].reverse().find((t) => t.name === e.name && t.result === null);
      if (call) call.result = e.result;
      const r = e.result;
      const txt = r && r.results ? `${r.results.length} výsledků: ${r.results.slice(0, 2).map((x) => esc(x.title)).join(" · ")}`
        : r && r.error ? `chyba ${esc(r.error)}` : esc(short(r, 90));
      log("RES", `<code>${esc(e.name)}</code> → ${txt}`, e.is_error ? "warn" : "");
      break;
    }
    case "usage":
      a.usage = e; updateCard(e.agent);
      break;
    case "agent_output":
      if (a.invocations.at(-1)) a.invocations.at(-1).output = e.output;
      break;
    case "handoff": {
      state.agents[e.to].handoffsIn.push(e);
      const ok = e.contract_ok;
      setEdge(e.from, e.to, ok ? "passed" : "returned", ok ? "OK" : "CHYBA");
      log("HND", `Handoff <b>${label(e.from)} → ${label(e.to)}</b>: kontrakt ${ok ? "OK" : "CHYBA: " + esc(e.missing.join("; "))}`, ok ? "handoff" : "handoff warn");
      break;
    }
    case "validation_result": {
      state.validations.push(e);
      const who = e.code_check.ok ? (e.agent_check && e.agent_check.ok === false ? "přijímající agent" : "") : "kontrakt v kódu";
      log("VAL", `Validace <b>${esc(e.gate)}</b> (pokus ${e.attempt}): ${esc(e.accepted)} → ${esc(e.next_action)}${who ? ` · zjistil: ${who}` : ""}`,
        e.accepted === "accepted" ? "handoff" : "handoff warn");
      if (e.accepted === "partial" && e.to !== "user") setEdge(e.from, e.to, "returned", "ČÁST.");
      break;
    }
    case "handoff_return":
      setEdge(e.to, e.from, "returned", "");
      log("RET", `<b>${label(e.from)}</b> vrací práci <b>${label(e.to)}</b>: ${esc((e.missing || []).join("; "))}`, "handoff warn");
      break;
    case "log":
      log("LOG", esc(e.msg), e.level === "warn" ? "warn" : "");
      break;
    case "final":
      renderAnswer(e);
      log("FIN", `${esc(STATUS_LABEL[e.status] || e.status)} za ${String(e.duration_s).replace(".", ",")} s · odhad ceny API ${usd(e.cost_usd)}`,
        e.status === "COMPLETED" ? "good" : e.status === "FAILED" ? "err" : "warn");
      $("#runTag").textContent = STATUS_LABEL[e.status] || e.status;
      $("#runTag").className = `tag ${STATUS_TAG[e.status] || ""}`;
      break;
    case "error":
      log("ERR", esc(e.msg), "err");
      for (const k in state.agents) if (state.agents[k].status === "working") { state.agents[k].status = "error"; updateCard(k); }
      if (!state.runId && pending()) { pending().classList.add("error"); pending().textContent = "CHYBA · " + e.msg; }
      break;
    case "run_finished":
      state.finished = true;
      source?.close();
      $("#send").disabled = false;
      break;
  }
}

// ---------- answer ----------
const pending = () => $(".msg.bot.pending");

const okTag = (ok) => `<span class="tag ${ok ? "good" : "bad"}">${ok ? "OK" : "CHYBA"}</span>`;
const num = (n) => (n == null ? "–" : String(n).replace(".", ","));

function evidenceHtml(e) {
  if (!e.ledger || !e.ledger.length) return "";
  const facts = e.ledger.map((f) => `<li class="rule"><div class="line"><span class="tag ${VSTATUS_TAG[f.verification_status] || ""}">${esc(VSTATUS_CZ[f.verification_status] || f.verification_status)}</span>
      <b>${esc(f.fact_id)}</b> ${esc(f.statement)}</div>
      <div class="why">JISTOTA ${esc(String(f.confidence).toUpperCase())}${f.independent_corroboration ? " · nezávisle potvrzeno" : ""}${f.limitations.length ? " · " + esc(f.limitations.join(", ")) : ""}</div>
      <ul>${f.evidence.map((x) => `<li><span class="tag ${x.quote_verified ? "good" : "warn"}">${x.quote_verified ? "CITÁT OK" : "CITÁT ?"}</span> <span class="note">${esc(x.quote_check)}</span> „${esc(short(x.quote, 220))}“ ${x.source_n ? `<a href="#src-${x.source_n}" class="cite">[${x.source_n}]</a>` : ""}</li>`).join("")}</ul></li>`).join("");
  return `<details class="evidence"><summary>#DŮKAZY · ${e.ledger.length} FAKTŮ</summary>
    <p class="note">CITÁT OK = citát nalezen v textu stažené stránky (neznamená, že je pravdivý). Tier zdroje je jen apriorní odhad podle URL.</p><ul class="checks">${facts}</ul></details>`;
}

function statsHtml(e) {
  const ev = e.evidence, ledger = e.ledger || [];
  const verifiedQuotes = ledger.reduce((n, f) => n + f.evidence.filter((x) => x.quote_verified).length, 0);
  const allQuotes = ledger.reduce((n, f) => n + f.evidence.length, 0);
  const cell = (v, l, c = "") => `<div class="stat"><b class="${c}">${v}</b><span>${l}</span></div>`;
  const sup = ev ? ev.evidence_support : null;
  return `<div class="stats">
    ${cell(ledger.length, "FAKTŮ")}
    ${cell(`${verifiedQuotes}/${allQuotes}`, "OVĚŘENÉ CITÁTY", verifiedQuotes === allQuotes && allQuotes ? "g" : "w")}
    ${cell(sup ? `${sup.supported}/${sup.of}` : "–", "PODLOŽENO", sup && sup.of && sup.supported === sup.of ? "g" : "w")}
    ${cell(ev ? ev.independent_corroboration : "–", "NEZÁVISLE")}
    ${cell((e.sources || []).length, "ZDROJŮ")}
    ${cell(usd(e.cost_usd).replace(" USD", "") || "–", "CENA USD")}</div>`;
}

function renderAnswer(e) {
  const el = pending() || document.createElement("div");
  el.className = "msg bot";
  const md = (e.answer_markdown || "").replace(/\[(\d+)\]/g, (m, n) => `<a class="cite" href="#src-${n}">[${n}]</a>`);
  const html = DOMPurify.sanitize(marked.parse(md), { ADD_ATTR: ["target"] });
  const tierTag = { A: "good", B: "strong", C: "warn", D: "bad" };
  const sources = (e.sources || []).map((s) => `<li id="src-${s.n}"><span class="n">[${s.n}]</span>${s.tier ? `<span class="tag ${tierTag[s.tier] || ""}" title="apriorní skóre ${s.score ?? "?"}">TIER ${s.tier}</span>` : ""}
      <a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></li>`).join("");
  const q = e.quality;
  const checks = q ? q.checks.map((c) => `<li class="rule"><div class="line">${okTag(c.ok)} ${esc(c.name)}</div>${c.detail ? `<div class="${c.ok ? "why" : "fix"}">${esc(c.detail)}</div>` : ""}</li>`).join("") : "";
  const usage = Object.entries(e.usage || {}).map(([k, u]) => `${label(k).toUpperCase()} ${u.model.replace("claude-", "")} · ${(u.input_tokens + u.output_tokens).toLocaleString("cs-CZ")} tok · ${u.web_searches || 0} hledání · ${usd(u.cost_usd)}`).join("<br>");
  const reasons = (e.status_reasons || []).length ? `<ul class="reasons">${e.status_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  el.innerHTML = `<div class="runhead"><span class="tag ${STATUS_TAG[e.status] || ""}">${esc(STATUS_LABEL[e.status] || e.status)}</span>
      <span class="note">SLOŽITOST ${esc(String(e.complexity || "–").toUpperCase())} · ${num(e.duration_s)} s</span></div>${reasons}
    ${statsHtml(e)}
    ${html ? `<div class="prose">${html}</div>` : ""}
    ${evidenceHtml(e)}
    ${sources ? `<div class="sources"><h3>#ZDROJE</h3><p class="note">Tier A–D = apriorní skóre ze skillu source-evaluation.</p><ol>${sources}</ol></div>` : ""}
    ${q ? `<details ${q.passed ? "" : "open"}><summary>#KONTROLA CITACÍ · ${q.passed ? "OK" : "POZOR"} · ${q.words} SLOV · CITOVÁNO ${q.cited.length}/${(e.sources || []).length}</summary><p class="note">Ověřuje jen formu (citace míří na zdroj, sekce Co nevíme), ne pravdivost.</p><ul class="checks">${checks}</ul></details>` : ""}
    <div class="runmeta">ODHAD CENY API ${usd(e.cost_usd)} (z ceníku v .env)<br>${esc(usage).replace(/&lt;br&gt;/g, "<br>")}</div>`;
}

function setupTheme() {
  const btn = $("#themeBtn");
  const current = () => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const paint = () => { btn.textContent = current() === "dark" ? "LIGHT" : "DARK"; };
  btn.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ui:theme", next); } catch (err) { /* private mode */ }
    paint();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", paint);
  paint();
}

// ---------- inspector ----------
let inspectorKey = null;
const TABS = [["system", "#SYSTEM"], ["calls", "#API VOLÁNÍ"], ["messages", "#KONVERZACE"], ["tools", "#TOOLS"], ["output", "#VÝSTUP"], ["validation", "#VALIDACE"], ["handoff", "#HANDOFF IN"], ["skill", "#SKILL"]];

function openInspector(key, tab = "calls") {
  inspectorKey = key;
  $("#drawerTitle").textContent = `#${label(key).toUpperCase()}`;
  $("#tabs").innerHTML = TABS.map(([k, l]) => `<button data-tab="${k}">${l}</button>`).join("");
  $("#tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#drawer").classList.remove("hidden");
  showTab(tab);
}

function blockHtml(b) {
  if (b.type === "text") return `<pre>${esc(b.text)}</pre>`;
  if (b.type === "tool_use" || b.type === "server_tool_use") return `<div class="note">${b.type} · ${esc(b.name)}</div><pre>${esc(json(b.input))}</pre>`;
  if (b.type === "tool_result") return `<div class="note">tool_result${b.is_error ? " (error)" : ""}</div><pre>${esc(short(b.content, 1500))}</pre>`;
  if (b.type === "web_search_tool_result") {
    const c = Array.isArray(b.content) ? b.content.map((r) => `${r.title} — ${r.url}`).join("\n") : json(b.content);
    return `<div class="note">web_search_tool_result</div><pre>${esc(c)}</pre>`;
  }
  return `<pre>${esc(json(b))}</pre>`;
}
const msgsHtml = (msgs) => msgs.map((m) => `<div class="msgblock"><div class="role">${m.role}</div>${
  typeof m.content === "string" ? `<pre>${esc(m.content)}</pre>` : m.content.map(blockHtml).join("")}</div>`).join("");

function validationHtml(v) {
  const ac = v.agent_check;
  return `<div class="msgblock"><div class="role">${esc(v.gate)} · pokus ${v.attempt} · ${esc(v.accepted)} → ${esc(v.next_action)}</div>
    <ul class="checks"><li class="rule"><div class="line">${okTag(v.code_check.ok)} Kontrakt v kódu</div>${v.code_check.missing.length ? `<div class="fix">${esc(v.code_check.missing.join("; "))}</div>` : ""}</li>
    <li class="rule"><div class="line">${ac ? okTag(ac.ok !== false) : `<span class="tag">–</span>`} Kontrola přijímajícím agentem</div>${ac ? (ac.missing && ac.missing.length ? `<div class="fix">${esc(ac.missing.join("; "))}</div>` : "") : `<div class="why">Neběžela, kontrakt už selhal.</div>`}</li></ul>
    ${v.evidence ? `<pre>${esc(json(v.evidence))}</pre>` : ""}</div>`;
}

async function showTab(tab) {
  $("#tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const a = state?.agents[inspectorKey];
  const inv = a?.invocations || [];
  const body = $("#drawerBody");
  if (!inv.length && !["skill", "validation"].includes(tab)) { body.innerHTML = `<p class="note">Agent zatím neběžel. Spusť dotaz.</p>`; return; }
  const head = (i, x) => inv.length > 1 ? `<h3>Spuštění ${i + 1}/${inv.length} · ${esc(x.model)}</h3>` : "";
  if (tab === "system") body.innerHTML = inv.map((x, i) => head(i, x) + `<pre>${esc(x.system)}</pre>`).join("");
  if (tab === "calls") body.innerHTML = `<p class="note">Každý POST na <code>/v1/messages</code> zvlášť: přesný (redigovaný) snímek pole <code>messages</code> v okamžiku odeslání a odpověď. Šifrovaný obsah vyhledávání zůstává jen v požadavku na API.</p>` +
    a.calls.map((c, i) => `<details ${i === a.calls.length - 1 ? "open" : ""}><summary><b>Request ${i + 1}</b> · ${esc(c.request.model)} · ${c.request.messages.length} zpráv · tools: ${esc(c.request.tools.join(", ") || "–")}
      ${c.response ? ` → <b>Response ${i + 1}</b>: stop_reason <code>${esc(c.response.stop_reason)}</code>, ${c.response.duration_ms} ms, ${(c.response.usage.input_tokens || 0) + (c.response.usage.output_tokens || 0)} tok` : " → čeká"}</summary>
      ${msgsHtml(c.request.messages)}</details>`).join("");
  if (tab === "messages") body.innerHTML = `<p class="note">Konverzace po doběhnutí agenta (včetně jeho odpovědí a tool_result).</p>` +
    inv.map((x, i) => head(i, x) + msgsHtml(x.final || x.messages) + `<details><summary class="note">Raw JSON</summary><pre>${esc(json(x.final || x.messages))}</pre></details>`).join("<hr>");
  if (tab === "tools") body.innerHTML = `<h3>Definice nástrojů (tools)</h3><pre>${esc(json(inv.at(-1).tools))}</pre><h3>Volání</h3>` +
    (a.tools.length ? a.tools.map((t) => `<div class="msgblock"><div class="role">${t.kind} · ${esc(t.name)}</div><pre>${esc(json(t.input))}</pre><pre>${esc(typeof t.result === "string" ? t.result : json(t.result))}</pre></div>`).join("")
      : `<p class="note">Tento agent nemá nástroje.</p>`);
  if (tab === "output") body.innerHTML = inv.map((x, i) => head(i, x) + `<pre>${esc(json(x.output))}</pre>`).join("");
  if (tab === "validation") {
    const vs = (state?.validations || []).filter((v) => v.to === inspectorKey || v.from === inspectorKey);
    body.innerHTML = vs.length ? `<p class="note">Každý pokus o předání: kontrakt v kódu, kontrola přijímajícím agentem a rozhodnutí orchestrátoru.</p>` + vs.map(validationHtml).join("")
      : `<p class="note">Pro tohoto agenta zatím žádná validace.</p>`;
  }
  if (tab === "handoff") body.innerHTML = a.handoffsIn.length ? a.handoffsIn.map((h) =>
    `<div class="msgblock"><div class="role">${label(h.from)} → ${label(h.to)} · kontrakt ${h.contract_ok ? "OK" : "CHYBA: " + esc(h.missing.join("; "))}</div><pre>${esc(json(h.payload))}</pre></div>`).join("")
    : `<p class="note">Router dostává přímo otázku uživatele.</p>`;
  if (tab === "skill") {
    const names = CONFIG.agents.find((x) => x.key === inspectorKey).skills;
    if (!names.length) { body.innerHTML = `<p class="note">Tento agent nepoužívá skill.</p>`; return; }
    const sr = a?.skillResult;
    const texts = await Promise.all(names.map((n) => fetch(`/api/skills/${n}`).then((r) => r.text())));
    body.innerHTML = (sr ? `<p><b>Vykonání:</b> <code>${esc(sr.tool)}</code> ${sr.invocations}× · pokrytí ${sr.sources_total - sr.not_scored.length}/${sr.sources_total} zdrojů${sr.not_scored.length ? " · chybí: " + esc(sr.not_scored.join(", ")) : ""}</p>`
      : `<p class="note">Skill je načtený do system promptu${a?.skills.size ? "" : " (agent zatím neběžel)"}; vykonání skriptu se hlásí zvlášť.</p>`) +
      texts.map((t, i) => `<h3>#${esc(names[i])} · SKILL.md</h3><pre>${esc(t)}</pre>`).join("");
  }
}

// ---------- run ----------
let hasSession = false;

async function ensureSession() {
  if (hasSession) return true;
  const r = await fetch("/api/session", { method: "POST", headers: { "content-type": "application/json" },
    credentials: "same-origin", body: JSON.stringify({ code: $("#code").value }) });
  hasSession = r.ok;
  return r.ok;
}

async function start(question) {
  if (source) source.close();
  state = freshState();
  renderPipeline();
  $("#log").innerHTML = "";
  $("#runTag").textContent = "BĚŽÍ"; $("#runTag").className = "tag hl";
  $("#empty")?.remove();
  $("#send").disabled = true;
  const thread = $("#thread");
  thread.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(question)}</div><div class="msg bot pending">BĚŽÍ · agenti pracují, sleduj karty nahoře a #PRŮBĚH.</div>`);
  thread.scrollTop = thread.scrollHeight;
  const fail = (msg) => { onEvent({ type: "error", msg }); $("#send").disabled = false; };
  if (!(await ensureSession())) return fail("Špatný přístupový kód.");
  let r = await fetch("/api/jobs", { method: "POST", headers: { "content-type": "application/json" },
    credentials: "same-origin", body: JSON.stringify({ question }) });
  if (r.status === 401) { hasSession = false; if (await ensureSession()) r = await fetch("/api/jobs", { method: "POST", headers: { "content-type": "application/json" }, credentials: "same-origin", body: JSON.stringify({ question }) }); }
  if (!r.ok) { const d = await r.json().catch(() => ({})); return fail(d.detail || `Chyba ${r.status}`); }
  const { job_id } = await r.json();
  source = new EventSource(`/api/jobs/${job_id}/events`);
  source.onmessage = (m) => { state.errors = 0; onEvent(JSON.parse(m.data)); };
  source.onerror = () => {
    if (state.finished) return source.close();
    state.errors += 1;  // the browser reconnects by itself and resumes from Last-Event-ID
    if (state.errors === 1) log("NET", "Spojení přerušeno, obnovuji a navazuji tam, kde jsem skončil…", "warn");
    if (state.errors > 6) { source.close(); fail("Spojení se serverem se nepodařilo obnovit. Běh pokračuje na serveru; log najdeš přes odkaz nahoře."); }
  };
}

function renderHow() {
  const plan = CONFIG.model_plan;
  const rows = Object.entries(plan).map(([k, v]) => `<tr><td>${k}</td><td>${CONFIG.models[v.researcher]}</td><td>${CONFIG.models[v.analyst]}</td><td>${CONFIG.models[v.writer]}</td><td>${v.searches}</td></tr>`).join("");
  $("#howBody").innerHTML = `
    <p><b>Sekvenční pipeline v kódu</b> (<code>app/orchestrator.py</code>): Router → Researcher → Analyst → Writer → kontrola výstupu.
    Agenti spolu nemluví napřímo; data mezi nimi předává jen orchestrátor.</p>
    <p><b>Handoff:</b> každý agent vrací JSON. Orchestrátor ho zkontroluje proti kontraktu (<code>app/contracts.py</code>) a přijímající agent
    navíc dělá vlastní <code>handoff_check</code>. Když něco chybí, práce se jednou vrátí zpět (RET v #PRŮBĚH).</p>
    <p><b>Router</b> (vždy ${esc(CONFIG.models.fast)}) určí složitost; kód podle ní vybere modely a počet vyhledávání:</p>
    <table><tr><th>složitost</th><th>Researcher</th><th>Analyst</th><th>Writer</th><th>hledání</th></tr>${rows}</table>
    <p><b>Nástroje:</b> Researcher – <code>web_search</code> (server tool Anthropic) a <code>fetch_page</code> (náš klient, trafilatura).
    Analyst – <code>evaluate_source</code> (skript skillu) a omezený <code>web_search</code> na ověření. Writer – bez nástrojů.</p>
    <p><b>Skills:</b></p><ul>${CONFIG.skills.map((s) => `<li><code>${esc(s.name)}</code> – ${esc(s.description)}</li>`).join("")}</ul>
    <p><b>Výsledek</b> má stav: <span class="tag good">HOTOVO</span>, <span class="tag warn">ČÁSTEČNĚ</span>, <span class="tag warn">MÁLO DŮKAZŮ</span> nebo <span class="tag bad">CHYBA</span>. Po vyčerpání vrácení se nikdy netváří jako hotový.</p>
    <p><b>Důkazy:</b> každé zjištění má doslovný citát; kód ho hledá v textu stažené stránky (CITÁT OK / CITÁT ?). Writer smí citovat jen fakta z ledgeru a kód kontroluje, že každé [n] patří k použitému faktu.</p>
    <p>Klikni na kartu agenta: system prompt, každé API volání zvlášť (request/response), finální konverzace, tools, výstup, validace předání a skill.</p>
    ${CONFIG.mock ? `<p class="note">Běží mock režim (bez API klíče): odpovědi mají stejný tvar jako API, orchestrace i tool loop běží naostro.</p>` : ""}`;
}

async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  $("#mode").textContent = CONFIG.mock ? "MOCK REŽIM" : "LIVE · CLAUDE API";
  $("#mode").className = `tag ${CONFIG.mock ? "warn" : "good"}`;
  setupTheme();
  if (CONFIG.access_code_required) $("#codeField").classList.remove("hidden");
  state = freshState();
  renderPipeline();
  renderHow();
  $("#examples").innerHTML = EXAMPLES.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("#examples").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { $("#q").value = b.textContent; }));
  $("#ask").addEventListener("submit", (ev) => { ev.preventDefault(); const q = $("#q").value.trim(); if (q.length >= 3) { start(q); $("#q").value = ""; } });
  $("#q").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#ask").requestSubmit(); } });
  $("#drawerClose").addEventListener("click", () => $("#drawer").classList.add("hidden"));
  $("#howBtn").addEventListener("click", () => $("#how").classList.remove("hidden"));
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => $("#" + b.dataset.close).classList.add("hidden")));
  document.querySelectorAll(".drawer").forEach((d) => d.addEventListener("click", (ev) => { if (ev.target === d) d.classList.add("hidden"); }));
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") document.querySelectorAll(".drawer").forEach((d) => d.classList.add("hidden")); });
}
init();

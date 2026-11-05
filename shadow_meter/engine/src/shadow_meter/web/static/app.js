// Shadow Meter UI. No framework, no external resources. All data is inserted with textContent.
"use strict";

// Standalone mode: the API token can be passed in the URL fragment (#token=...). The fragment is never
// sent to the server, so the token does not end up in logs or referrers.
const TOKEN = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const HEADERS = TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};

const state = {
  status: null,
  candidates: new Map(), // id -> candidate
  types: {},
  filter: "all",
  showUncertain: false,
  lastPhase: null,
};

const $ = (id) => document.getElementById(id);
const pct = (x) => `${Math.round((x || 0) * 100)} %`;
const fmtW = (w) => (w == null ? "–" : w >= 1000 ? (w / 1000).toFixed(2) : w >= 100 ? w.toFixed(0) : w.toFixed(1));
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString() : "–");
const fmtTime = (d = new Date()) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { ...HEADERS, ...(options.headers || {}) } });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---- activity feed -------------------------------------------------------------------------------
function feed(text) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = fmtTime();
  li.append(time, document.createTextNode(text));
  li.className = "fresh";
  const list = $("feed-list");
  list.prepend(li);
  while (list.children.length > 50) list.lastChild.remove();
  $("feed").hidden = false;
}

// ---- status --------------------------------------------------------------------------------------
function renderStatus(s) {
  state.status = s;
  const dot = $("status-dot");
  dot.className = "dot " + ({ live: "live", backfill: "busy", starting: "busy", error: "error" }[s.state] || "");
  let text = s.message || s.state;
  if (s.state === "live" && s.live_since) text = `Live since ${fmtTime(new Date(s.live_since * 1000))}`;
  $("status-text").textContent = text;

  const b = s.backfill;
  const show = s.state === "backfill" || (b && b.percent < 100);
  $("progress").hidden = !(show || s.steps);
  if (b) {
    $("progress-title").textContent = s.state === "backfill" ? s.message : "History analysed";
    $("progress-detail").textContent =
      s.state === "backfill" ? `${fmtDate(b.current)} · ${b.percent.toFixed(1)} %` : `${fmtDate(b.from)} – ${fmtDate(b.to)}`;
    $("progress-fill").style.width = `${b.percent}%`;
  }
  $("count-steps").textContent = (s.steps || 0).toLocaleString();
  $("count-cycles").textContent = (s.cycles || 0).toLocaleString();
  if (state.lastPhase && state.lastPhase !== s.state) {
    feed(s.state === "live" ? "History done – now following live data" : `Status: ${s.message || s.state}`);
  }
  state.lastPhase = s.state;
}

// ---- cards ---------------------------------------------------------------------------------------
function visible(c) {
  if (state.filter !== "all" && c.status !== state.filter) return false;
  if (state.filter === "all" && c.status === "rejected") return false;
  if (!state.showUncertain && c.status === "new" && c.confidence < 0.2) return false;
  // probably part of another device (a fragment of a fridge, the power of a light): only on request
  if (!state.showUncertain && c.status === "new" && c.explained_by) return false;
  return true;
}

function facts(c) {
  const f = c.features || {};
  const rows = [];
  if (c.source === "entity") {
    rows.push(["Entity", c.entity_id]);
    rows.push(["Matched", `${pct(f.match_rate)} of switch events (chance: ${pct(f.chance_rate)})`]);
  } else {
    rows.push(["Cycles", `${Math.round(f.count || 0).toLocaleString()} (${(f.cycles_per_day || 0).toFixed(1)}/day)`]);
    rows.push(["Runs for", `${(f.duration_min || 0).toFixed(1)} min`]);
    rows.push(["Running", `${pct(f.duty)} of the time`]);
    if (f.q_ratio != null && Number.isFinite(f.q_ratio)) {
      const kind = f.q_ratio < 0.08 ? "resistive, like heating" : "inductive, like a motor or power supply";
      rows.push(["Reactive", `ΔQ/ΔP ${f.q_ratio.toFixed(2)} (${kind})`]);
    }
    rows.push(["Seen", `${fmtDate(c.first_seen)} – ${fmtDate(c.last_seen)}`]);
  }
  return rows;
}

function renderCard(el, c) {
  el.dataset.id = c.id;
  el.classList.toggle("rejected", c.status === "rejected");
  el.querySelector(".type").textContent = (state.types[c.type] || c.type).split(" (")[0];
  el.querySelector(".phase").textContent = c.phase ? `Phase ${c.phase}` : "Phase ?";
  el.querySelector(".running").hidden = !(c.status === "active" && c.running);
  el.querySelector(".state").textContent =
    { new: "new", confirmed: "confirmed", rejected: "rejected", active: "in Home Assistant" }[c.status];
  const name = el.querySelector(".name");
  if (document.activeElement !== name) name.value = c.name;
  el.querySelector(".watts").textContent = fmtW(c.power_w);
  el.querySelector(".unit").textContent = c.power_w >= 1000 ? "kW" : "W";
  const kwhDay = c.features && c.features.kwh_per_day;
  el.querySelector(".kwh").textContent =
    c.status === "active" ? `${(c.energy_kwh || 0).toFixed(2)} kWh counted` : kwhDay ? `≈ ${kwhDay.toFixed(2)} kWh/day` : "";

  el.querySelector(".conf-value").textContent = pct(c.confidence);
  const fill = el.querySelector(".confidence .bar-fill");
  fill.style.width = `${Math.round(c.confidence * 100)}%`;
  fill.className = "bar-fill " + (c.confidence >= 0.7 ? "high" : c.confidence >= 0.4 ? "mid" : "low");
  const hist = (c.history || []).map((p) => p[1]);
  const trend = el.querySelector(".trend");
  const delta = hist.length >= 2 ? hist[hist.length - 1] - hist[Math.max(0, hist.length - 5)] : 0;
  trend.textContent = delta > 0.02 ? "▲" : delta < -0.02 ? "▼" : "";
  trend.className = "trend " + (delta > 0.02 ? "up" : delta < -0.02 ? "down" : "");
  trend.title = delta ? `${delta > 0 ? "+" : ""}${Math.round(delta * 100)} points recently` : "";
  // x by time: the history covers the whole lifetime of the candidate (thinned out with age)
  const times = (c.history || []).map((p) => p[0]);
  const t0 = times[0];
  const span = times.length >= 2 ? times[times.length - 1] - t0 : 0;
  const pts = span > 0 ? hist.map((v, i) => `${((times[i] - t0) / span) * 100},${22 - v * 20}`).join(" ") : "";
  el.querySelector(".spark polyline").setAttribute("points", pts);
  el.querySelector(".spark-axis").hidden = !(span > 0);
  if (span > 0) {
    const time = { hour: "2-digit", minute: "2-digit" };
    const date = { day: "numeric", month: "short", ...(span > 300 * 86400 ? { year: "2-digit" } : {}) };
    const fmt = (t) => {
      const d = new Date(t * 1000);
      if (span < 86400) return d.toLocaleTimeString([], time);
      if (span < 7 * 86400) return d.toLocaleString([], { ...date, ...time });
      return d.toLocaleDateString([], date);
    };
    el.querySelector(".spark-from").textContent = fmt(t0);
    el.querySelector(".spark-to").textContent = fmt(t0 + span);
  }

  const dl = el.querySelector(".facts");
  dl.replaceChildren();
  for (const [k, v] of facts(c)) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  const note = el.querySelector(".note");
  note.hidden = !c.explained_by;
  note.textContent = c.explained_by ? `Probably part of the same device as ${nameOf(c.explained_by)}.` : "";

  const sel = el.querySelector(".type-select");
  if (!sel.options.length) {
    for (const [k, label] of Object.entries(state.types)) sel.add(new Option(label, k));
    if (!state.types[c.type]) sel.add(new Option(c.type, c.type));
  }
  if (document.activeElement !== sel) sel.value = c.type;

  el.querySelector(".confirm").hidden = c.status !== "new";
  el.querySelector(".reject").textContent = c.status === "rejected" ? "Restore" : "Reject";
  el.querySelector(".activate").textContent = c.status === "active" ? "Remove from Home Assistant" : "Add to Home Assistant";
  el.querySelector(".activate").hidden = c.status === "rejected";
  const noMqtt = state.status && state.status.mqtt === "disabled" && c.status !== "active";
  el.querySelector(".activate").disabled = noMqtt;
  el.querySelector(".activate").title = noMqtt
    ? "No MQTT broker: install the Mosquitto app (or configure mqtt in the options) to add devices"
    : "";
}

function nameOf(id) {
  const c = state.candidates.get(id);
  return c ? c.name : id;
}

function renderCards() {
  const main = $("cards");
  const list = [...state.candidates.values()].filter(visible);
  const existing = new Map([...main.children].map((el) => [el.dataset.id, el]));
  for (const c of list) {
    let el = existing.get(c.id);
    if (!el) {
      el = $("card-template").content.firstElementChild.cloneNode(true);
      bindCard(el);
    }
    renderCard(el, c);
    main.append(el); // keeps the server's order (best confidence first)
    existing.delete(c.id);
  }
  for (const el of existing.values()) el.remove();
  $("empty").hidden = list.length > 0;
  $("count-candidates").textContent = state.candidates.size.toLocaleString();
}

function bindCard(el) {
  const id = () => el.dataset.id;
  const save = async (body) => {
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const updated = await api(`api/candidates/${encodeURIComponent(id())}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const c = { ...state.candidates.get(id()), ...updated };
      state.candidates.set(id(), c);
      renderCards();
    } catch (err) {
      feed(`Could not save: ${err.message}`);
    } finally {
      el.querySelectorAll("button").forEach((b) => (b.disabled = false));
      renderCards();
    }
  };
  el.querySelector(".confirm").addEventListener("click", () => save({ status: "confirmed" }));
  el.querySelector(".reject").addEventListener("click", () => {
    const c = state.candidates.get(id());
    save({ status: c.status === "rejected" ? "new" : "rejected" });
  });
  el.querySelector(".activate").addEventListener("click", () => {
    const c = state.candidates.get(id());
    const active = c.status === "active";
    save({ status: active ? "confirmed" : "active", name: el.querySelector(".name").value.trim() });
    feed(active ? `${c.name} removed from Home Assistant` : `${el.querySelector(".name").value.trim()} added to Home Assistant`);
  });
  const name = el.querySelector(".name");
  name.addEventListener("keydown", (e) => e.key === "Enter" && name.blur());
  name.addEventListener("change", () => save({ name: name.value.trim() }));
  el.querySelector(".type-select").addEventListener("change", (e) => save({ type: e.target.value }));
}

function updateCandidates(list) {
  const before = state.candidates;
  state.candidates = new Map(list.map((c) => [c.id, c]));
  for (const c of list) {
    const old = before.get(c.id);
    if (old && Math.abs(c.confidence - old.confidence) >= 0.05) {
      feed(`${c.name}: confidence ${c.confidence > old.confidence ? "rising" : "falling"} to ${pct(c.confidence)}`);
      requestAnimationFrame(() => {
        const el = document.querySelector(`.card[data-id="${CSS.escape(c.id)}"]`);
        if (el) {
          el.classList.add("flash");
          setTimeout(() => el.classList.remove("flash"), 1200);
        }
      });
    }
  }
  renderCards();
}

// ---- settings: choose the meters -----------------------------------------------------------------
const setup = { sensors: [], phases: [], suggestion: [], pfSensors: [], pfSuggestion: {}, findings: {} };

function sensorLabel(s) {
  const value = s.value == null ? "–" : `${fmtW(s.value)} W`;
  return `${s.name} (${value})`;
}

function renderPhases() {
  const box = $("setup-phases");
  box.replaceChildren();
  setup.phases.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "phase-row";
    const name = document.createElement("input");
    name.value = p.phase;
    name.maxLength = 16;
    name.setAttribute("aria-label", "Phase name");
    name.addEventListener("change", () => {
      p.phase = name.value.trim();
      renderSubs();
    });
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", `Meter of phase ${p.phase}`);
    sel.add(new Option("— choose the power sensor of this phase —", ""));
    for (const s of setup.sensors) sel.add(new Option(`${sensorLabel(s)} · ${s.entity_id}`, s.entity_id));
    sel.value = p.mains || "";
    // optional power factor sensor: adds the reactive power (heating vs. motor) to the detection
    const pf = document.createElement("select");
    pf.className = "pf-select";
    pf.setAttribute("aria-label", `Power factor sensor of phase ${p.phase} (optional)`);
    pf.add(new Option("— no power factor sensor (optional) —", ""));
    for (const s of setup.pfSensors) {
      const value = s.value == null ? "–" : Math.abs(s.value) > 1 ? `${s.value} %` : `${s.value}`;
      pf.add(new Option(`Power factor: ${s.name} (${value}) · ${s.entity_id}`, s.entity_id));
    }
    pf.value = p.power_factor || "";
    pf.hidden = !setup.pfSensors.length;
    pf.addEventListener("change", () => {
      p.power_factor = pf.value || null;
      $("use-pf").hidden = !setup.phases.some((q) => !q.power_factor && setup.pfSuggestion[q.mains]);
    });
    sel.addEventListener("change", () => {
      p.mains = sel.value;
      setup.phases.forEach((q) => (q.submeters = q.submeters.filter((e) => e !== p.mains)));
      if (!p.power_factor && setup.pfSuggestion[p.mains]) {
        p.power_factor = setup.pfSuggestion[p.mains];
        pf.value = p.power_factor;
      }
      $("use-pf").hidden = !setup.phases.some((q) => !q.power_factor && setup.pfSuggestion[q.mains]);
      renderSubs();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.disabled = setup.phases.length === 1;
    remove.addEventListener("click", () => {
      setup.phases.splice(i, 1);
      renderPhases();
      renderSubs();
    });
    row.append(name, sel, remove, pf);
    box.append(row);
  });
  $("add-phase").disabled = setup.phases.length >= 8;
  // offered when a phase has no power factor sensor yet but one obviously belongs to its meter
  $("use-pf").hidden = !setup.phases.some((p) => !p.power_factor && setup.pfSuggestion[p.mains]);
}

function findingText(f) {
  if (f.verdict === "phase") {
    const hits = Math.round((f.shares[f.phase] || 0) * f.steps);
    return `Found: phase ${f.phase} (${hits} of ${f.steps} switch events showed up there)`;
  }
  if (f.verdict === "not_behind") return "Not behind the whole-home meter (its switching never shows up there): leave it out";
  if (!f.steps) return "No switching seen in the history: it does not matter for the detection";
  return `Unclear (${f.steps} switch events): leave it out or choose the phase yourself`;
}

async function findPhases() {
  const phases = setup.phases.filter((p) => p.phase && p.mains).map((p) => ({ phase: p.phase, mains: p.mains }));
  if (!phases.length) return showSetupError("Choose the power sensors of the phases first.");
  showSetupError("");
  $("find-phases").disabled = true;
  try {
    const started = await api("api/setup/find-phases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phases }),
    });
    $("find-status").textContent = `Looking at ${started.days} days of history…`;
    let job;
    do {
      await new Promise((r) => setTimeout(r, 2000));
      job = await api("api/setup/find-phases");
      if (job.state === "running") $("find-status").textContent = `Looking at the history: day ${job.done_days} of ${job.days}…`;
    } while (job.state === "running");
    if (job.state === "error") throw new Error(job.message);
    setup.findings = job.results || {};
    let assigned = 0;
    for (const [eid, f] of Object.entries(setup.findings)) {
      if (f.verdict !== "phase" || setup.phases.some((p) => p.submeters.includes(eid))) continue;
      const target = setup.phases.find((p) => p.phase === f.phase);
      if (target) {
        target.submeters.push(eid);
        assigned++;
      }
    }
    const values = Object.values(setup.findings);
    const outside = values.filter((f) => f.verdict === "not_behind").length;
    $("find-status").textContent =
      `Assigned ${assigned} device${assigned === 1 ? "" : "s"}` +
      (outside ? `, ${outside} not behind the whole-home meter` : "") +
      ". Check the list and save.";
    renderSubs();
  } catch (err) {
    $("find-status").textContent = "";
    showSetupError(`Could not find the phases: ${err.message.replace(/^\d+ /, "")}`);
  } finally {
    $("find-phases").disabled = false;
  }
}

function renderSubs() {
  const box = $("setup-subs");
  const filter = $("sub-filter").value.trim().toLowerCase();
  const mains = new Set(setup.phases.map((p) => p.mains));
  box.replaceChildren();
  for (const s of setup.sensors) {
    if (mains.has(s.entity_id)) continue;
    if (filter && !`${s.name} ${s.entity_id}`.toLowerCase().includes(filter)) continue;
    const row = document.createElement("div");
    row.className = "sub-row";
    const label = document.createElement("div");
    label.textContent = sensorLabel(s);
    const id = document.createElement("small");
    id.textContent = s.entity_id;
    label.append(id);
    const finding = setup.findings[s.entity_id];
    if (finding) {
      const note = document.createElement("span");
      note.className = "finding";
      note.textContent = findingText(finding);
      label.append(note);
    }
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", `Phase of ${s.name}`);
    sel.add(new Option("not used", ""));
    for (const p of setup.phases) if (p.phase) sel.add(new Option(`Phase ${p.phase}`, p.phase));
    const owner = setup.phases.find((p) => p.submeters.includes(s.entity_id));
    sel.value = owner ? owner.phase : "";
    sel.addEventListener("change", () => {
      setup.phases.forEach((p) => (p.submeters = p.submeters.filter((e) => e !== s.entity_id)));
      const target = setup.phases.find((p) => p.phase === sel.value);
      if (target) target.submeters.push(s.entity_id);
    });
    row.append(label, sel);
    box.append(row);
  }
  if (!box.children.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "No other power sensors.";
    box.append(empty);
  }
}

function showSetupError(text) {
  $("setup-error").textContent = text;
  $("setup-error").hidden = !text;
}

async function openSettings() {
  $("setup").hidden = false;
  showSetupError("");
  try {
    const data = await api("api/setup");
    setup.sensors = data.sensors;
    setup.suggestion = data.suggestion;
    setup.pfSensors = data.pf_sensors || [];
    setup.pfSuggestion = data.pf_suggestion || {};
    setup.phases = data.phases.length
      ? data.phases.map((p) => ({ ...p, submeters: [...p.submeters] }))
      : data.suggestion.length
        ? data.suggestion.map((p) => ({ ...p, submeters: [] }))
        : [{ phase: "A", mains: "", submeters: [] }];
    const source = $("setup-source");
    source.hidden = data.source !== "options";
    source.textContent = "The meters are currently set in the options file. Saving here takes over.";
    $("history-box").hidden = !data.source; // only once an analysis exists
    askReanalyse(false);
    $("history-info").textContent = data.influx
      ? "An InfluxDB is linked. The analysis continues where it stopped; to include older history " +
        "(e.g. after linking the InfluxDB), analyse it again from the start."
      : `The history comes from the Home Assistant recorder (${data.history_days} days). Link an ` +
        "InfluxDB in the app configuration and restart the app to analyse years of history.";
    $("use-suggestion").hidden = !data.suggestion.length;
    $("use-suggestion").textContent = `Use suggested meter (${data.suggestion.length} phases)`;
    $("use-suggestion").title = data.suggestion.map((p) => `${p.phase}: ${p.mains}`).join("\n");
    renderPhases();
    renderSubs();
    $("setup").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showSetupError(`Could not load the sensors: ${err.message}`);
  }
}

async function saveSettings() {
  const phases = setup.phases.map((p) => ({
    phase: p.phase.trim(),
    mains: p.mains,
    submeters: p.submeters,
    power_factor: p.power_factor || null,
  }));
  if (phases.some((p) => !p.mains)) return showSetupError("Choose a power sensor for every phase.");
  $("setup-save").disabled = true;
  try {
    await api("api/setup", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phases }),
    });
    showSetupError("");
    $("setup-save").textContent = "Saved – restarting the analysis…";
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    showSetupError(err.message.replace(/^\d+ /, ""));
    $("setup-save").disabled = false;
  }
}

// no window.confirm(): it can be blocked silently inside the Home Assistant iframe
function askReanalyse(show) {
  $("reanalyse-confirm").hidden = !show;
  $("reanalyse").hidden = show;
}

async function reanalyse() {
  $("reanalyse-yes").disabled = true;
  try {
    await api("api/reanalyse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    $("reanalyse-yes").textContent = "Starting over…";
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    showSetupError(err.message.replace(/^\d+ /, ""));
    $("reanalyse-yes").disabled = false;
  }
}

function setSetupMode(needsSetup) {
  for (const id of ["progress", "feed"]) if (needsSetup) $(id).hidden = true;
  document.querySelector(".filters").hidden = needsSetup;
  $("cards").hidden = needsSetup;
  if (needsSetup) $("empty").hidden = true;
  $("setup-cancel").hidden = needsSetup;
}

// ---- live updates --------------------------------------------------------------------------------
let retry = 1000;
function connect() {
  const url = new URL("api/events", location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.hash = "";
  if (TOKEN) url.searchParams.set("token", TOKEN);
  const ws = new WebSocket(url);
  ws.onopen = () => (retry = 1000);
  ws.onmessage = (msg) => {
    const { kind, data } = JSON.parse(msg.data);
    if (kind === "status") renderStatus(data);
    else if (kind === "candidates") updateCandidates(data);
    else if (kind === "new") data.forEach((c) => feed(`New device candidate: ${c.name}`));
  };
  ws.onclose = () => {
    $("status-dot").className = "dot error";
    $("status-text").textContent = "Reconnecting…";
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 30000);
  };
}

async function init() {
  $("open-settings").addEventListener("click", openSettings);
  $("setup-cancel").addEventListener("click", () => ($("setup").hidden = true));
  $("setup-save").addEventListener("click", saveSettings);
  $("reanalyse").addEventListener("click", () => askReanalyse(true));
  $("find-phases").addEventListener("click", findPhases);
  $("reanalyse-no").addEventListener("click", () => askReanalyse(false));
  $("reanalyse-yes").addEventListener("click", reanalyse);
  $("add-phase").addEventListener("click", () => {
    const used = new Set(setup.phases.map((p) => p.phase));
    const name = ["A", "B", "C", "D", "E", "F", "G", "H"].find((n) => !used.has(n)) || "";
    setup.phases.push({ phase: name, mains: "", submeters: [], power_factor: null });
    renderPhases();
    renderSubs();
  });
  $("use-pf").addEventListener("click", () => {
    for (const p of setup.phases) if (!p.power_factor) p.power_factor = setup.pfSuggestion[p.mains] || null;
    renderPhases();
  });
  $("use-suggestion").addEventListener("click", () => {
    setup.phases = setup.suggestion.map((p) => ({ ...p, submeters: [] }));
    renderPhases();
    renderSubs();
  });
  $("sub-filter").addEventListener("input", renderSubs);
  document.querySelectorAll(".tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
      state.filter = b.dataset.filter;
      renderCards();
    }),
  );
  $("show-uncertain").addEventListener("change", (e) => {
    state.showUncertain = e.target.checked;
    renderCards();
  });
  try {
    const s = await api("api/status");
    state.types = s.types;
    renderStatus(s);
    setSetupMode(s.state === "setup");
    if (s.state === "setup") {
      await openSettings();
      return; // nothing to follow until the meters are chosen
    }
    updateCandidates(await api("api/candidates"));
  } catch (err) {
    $("status-text").textContent = `Error: ${err.message}`;
  }
  connect();
}

init();

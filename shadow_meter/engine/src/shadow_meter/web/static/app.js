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
  const pts = hist.length >= 2 ? hist.map((v, i) => `${(i / (hist.length - 1)) * 100},${22 - v * 20}`).join(" ") : "";
  el.querySelector(".spark polyline").setAttribute("points", pts);

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
  note.textContent = c.explained_by ? `Probably the same device as ${nameOf(c.explained_by)}.` : "";

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
    updateCandidates(await api("api/candidates"));
  } catch (err) {
    $("status-text").textContent = `Error: ${err.message}`;
  }
  connect();
}

init();

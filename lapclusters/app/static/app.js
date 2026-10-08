// LapClusters dashboard. No framework: the page asks the local server for the
// cluster's state once a second and redraws only the parts that changed.
//
// Everything that comes from a model, a file name or another laptop is put on
// the page as text, never as HTML.

const app = document.getElementById("app");
const toasts = document.getElementById("toasts");

const ui = {
  data: null,          // last /api/state reply
  serverDown: false,   // the local app itself stopped answering
  skew: 0,             // Redis clock minus this browser's clock, in seconds
  tab: "review",
  kind: "review",      // which kind of job the form is set to start
  attached: [],        // files chosen, dropped or pasted, waiting to be sent
  uploadNote: "",
  job: "",             // a past review chosen in History; "" follows the newest
  hosts: null,         // hosts found on the network; null until the first search
  password: null,
  connecting: false,
  connectProblem: "",
  reviewProblem: "",
  startingReview: false,
  pendingModel: {},    // laptop -> { model, until } while a remote switch lands
  taskId: null,        // file open in the side panel
  task: null,
  taskTab: null,       // null means "pick the natural tab for its status"
  output: { text: "", next: 0 },
  findings: null,
  findingsKey: "",
  filter: { high: true, medium: true, low: true, text: "" },
  lastEvent: null,
};

// --- small helpers ---------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "value" || key === "disabled" || key === "selected" || key === "checked") el[key] = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Rebuild a region only when what it shows has changed, so that typing in a
// field or holding a menu open is never interrupted by the next refresh.
function region(el, signature, build) {
  const key = typeof signature === "string" ? signature : JSON.stringify(signature);
  if (el.dataset.sig === key) return;
  el.dataset.sig = key;
  el.replaceChildren(...[build()].flat(Infinity).filter(Boolean));
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "content-type": "application/json", "x-lapclusters": "1" },
    body: JSON.stringify(body),
  };
  const reply = await fetch(path, options);
  let data = null;
  try { data = await reply.json(); } catch { /* not JSON */ }
  if (!reply.ok) throw new Error((data && data.error) || `The request failed (${reply.status}).`);
  return data;
}

function toast(text, bad = false) {
  const el = h("div", { class: bad ? "toast bad" : "toast", role: bad ? "alert" : "status", text });
  toasts.append(el);
  setTimeout(() => el.remove(), bad ? 9000 : 5000);
}

const now = () => Date.now() / 1000 + ui.skew;

function duration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "–";
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
}

function clock(stamp) {
  if (!stamp) return "–";
  return new Date(stamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function plural(count, word) { return `${count} ${word}${count === 1 ? "" : "s"}`; }

const STATUS_WORDS = { pending: "Waiting", running: "Reviewing", done: "Done", failed: "Failed" };

function status(kind, word) {
  return h("span", { class: `status ${kind}` }, h("span", { class: "glyph", "aria-hidden": "true" }), word || STATUS_WORDS[kind] || kind);
}

function counts(high, medium, low) {
  const parts = [];
  if (high) parts.push(h("span", { class: "sev high", text: String(high) }));
  if (medium) parts.push(h("span", { class: "sev medium", text: String(medium) }));
  if (low) parts.push(h("span", { class: "sev low", text: String(low) }));
  return parts.length ? h("span", { class: "counts" }, parts) : h("span", { class: "muted", text: "none" });
}

// A live "12s" that keeps counting between refreshes.
function since(stamp) { return h("span", { "data-since": String(stamp), text: duration(now() - stamp) }); }

setInterval(() => {
  for (const el of document.querySelectorAll("[data-since]")) {
    el.textContent = duration(now() - Number(el.dataset.since));
  }
}, 1000);

// --- theme -----------------------------------------------------------------

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("lapclusters-theme", theme); } catch { /* private mode */ }
}
function themeButton() {
  return h("button", {
    class: "btn quiet small", text: "Theme", title: "Switch between light and dark",
    onclick: () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
  });
}
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("lapclusters-theme"); } catch { /* private mode */ }
  applyTheme(saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
})();

// --- data loop -------------------------------------------------------------

async function refresh() {
  try {
    const data = await api(`/api/state${ui.job ? `?job=${encodeURIComponent(ui.job)}` : ""}`);
    ui.serverDown = false;
    if (data.now) ui.skew = data.now - Date.now() / 1000;
    // Offer the password this laptop already knows, until the user types one.
    // It is only sent while disconnected, so it has to be picked up whenever
    // it appears, not just on the first refresh.
    if (!ui.passwordTouched && data.saved_password) ui.password = data.saved_password;
    if (ui.password === null) ui.password = "";
    noticeEvents(data);
    ui.data = data;
  } catch {
    ui.serverDown = true;
  }
  render();
}

function noticeEvents(data) {
  const events = data.events || [];
  if (!events.length) return;
  if (ui.lastEvent !== null) {
    for (const event of events) {
      if (event.id === ui.lastEvent) break;
      // Tell this laptop's owner when someone else changed its model.
      if (event.kind === "model" && event.worker === data.me && event.by && event.by !== data.me) toast(event.text);
    }
  }
  ui.lastEvent = events[0].id;
}

async function searchHosts() {
  if (!ui.data || ui.data.role !== null) return;
  try { ui.hosts = (await api("/api/discover")).hosts; } catch { /* keep the last list */ }
  render();
}

setInterval(refresh, 1000);
setInterval(searchHosts, 4000);
refresh().then(searchHosts);

// --- top level -------------------------------------------------------------

function render() {
  if (!ui.data) {
    if (ui.serverDown) app.replaceChildren(h("p", { class: "boot", text: "Waiting for the LapClusters app on this laptop…" }));
    return;
  }
  const view = ui.data.role === null ? "connect" : "cluster";
  if (app.dataset.view !== view) {
    app.dataset.view = view;
    app.replaceChildren(...[view === "connect" ? buildConnect() : buildCluster()].flat());
    if (view === "connect") { closeTask(); ui.job = ""; ui.tab = "review"; }
  }
  if (view === "connect") drawConnect(); else drawCluster();
}

// --- pictures --------------------------------------------------------------

// Fixed markup written here, never built from data, so it is safe to parse.
const ICONS = {
  logo: '<svg viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="currentColor"/><g stroke="var(--accent-ink)" stroke-width="1.8" stroke-linecap="round"><path d="M16 10v6M16 16l-6 5M16 16l6 5"/></g><g fill="var(--accent-ink)"><circle cx="16" cy="9" r="2.7"/><circle cx="9.5" cy="22" r="2.7"/><circle cx="22.5" cy="22" r="2.7"/><circle cx="16" cy="16" r="1.9"/></g></svg>',
  shield: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5.5c0 4.2-2.9 7.6-7 9.5-4.1-1.9-7-5.3-7-9.5V6l7-3z"/><path d="M9 12l2.2 2.2L15.2 10"/></svg>',
  wallet: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7.5A2.5 2.5 0 016.5 5H18v3"/><path d="M4 7.5V17a2 2 0 002 2h13a1 1 0 001-1V9a1 1 0 00-1-1H6.5A2.5 2.5 0 014 7.5z"/><circle cx="16.5" cy="13.5" r="1.2" fill="currentColor" stroke="none"/></svg>',
  bolt: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 3L5 13.5h6L10 21l8-10.5h-6L13 3z"/></svg>',
  diagram: '<svg class="diagram" viewBox="0 0 440 250" role="img" aria-label="Four laptops, each running its own model, sharing work through one queue"><path class="wire" d="M220 125L84 62"/><path class="wire" d="M220 125L356 62"/><path class="wire" d="M220 125L84 190"/><path class="wire" d="M220 125L356 190"/>LAPTOPS<circle class="halo" cx="220" cy="125" r="44"/><circle class="hub" cx="220" cy="125" r="31"/><text x="220" y="129" text-anchor="middle">QUEUE</text><text class="cap" x="84" y="110" text-anchor="middle">own model</text><text class="cap" x="356" y="110" text-anchor="middle">own model</text><text class="cap" x="84" y="238" text-anchor="middle">own model</text><text class="cap" x="356" y="238" text-anchor="middle">own model</text></svg>',
};

// One laptop drawing, placed four times. Written out in full for each place,
// because styles do not reach inside an SVG <use> copy.
// Each laptop has a small face that blinks, and bobs a little out of step with
// the others.
const LAPTOP = '<rect class="screen" x="-36" y="-26" width="72" height="44" rx="6"/><rect class="base" x="-46" y="21" width="92" height="6" rx="3"/><circle class="eye" cx="-10" cy="-9" r="2.8"/><circle class="eye" cx="10" cy="-9" r="2.8"/><path class="smile" d="M-8 2Q0 9 8 2"/>';
const SPOTS = [[84, 62], [356, 62], [84, 190], [356, 190]];
// Little parcels of work: one travels from the queue to each laptop, and an
// answer travels back, at staggered times so the picture is never still.
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const parcels = still ? "" : SPOTS.map(([x, y], i) => (
  `<circle class="parcel out" r="4.2"><animateMotion dur="2.6s" begin="${i * 0.65}s" repeatCount="indefinite" path="M220 125L${x} ${y}" keyPoints="0.18;0.8" keyTimes="0;1" calcMode="linear"/></circle>`
  + `<circle class="parcel back" r="3.4"><animateMotion dur="2.6s" begin="${i * 0.65 + 1.3}s" repeatCount="indefinite" path="M${x} ${y}L220 125" keyPoints="0.2;0.82" keyTimes="0;1" calcMode="linear"/></circle>`
)).join("");
ICONS.diagram = ICONS.diagram.replace(
  "LAPTOPS",
  parcels + SPOTS.map(([x, y], i) => (
    `<g transform="translate(${x} ${y})"><g class="lap" style="animation-delay:${-i * 0.7}s">${LAPTOP.replaceAll('class="eye"', `class="eye" style="animation-delay:${-i * 1.1}s"`)}</g></g>`
  )).join(""),
);

function icon(name) {
  const holder = document.createElement("template");
  holder.innerHTML = ICONS[name];
  const el = holder.content.firstElementChild;
  if (!el.hasAttribute("role")) el.setAttribute("aria-hidden", "true");
  return el;
}

function brand() { return h("span", { class: "brand" }, icon("logo"), "LapClusters"); }

// --- connect screen --------------------------------------------------------

let connectEls = null;

function buildConnect() {
  const password = h("input", {
    // "new-password" stops the browser filling in a password saved for some
    // other site, which then fails here with no visible reason.
    class: "field", type: "password", autocomplete: "new-password", placeholder: "Leave empty if Redis has none",
    "aria-label": "Cluster password",
    value: ui.password || "", oninput: (e) => { ui.password = e.target.value; ui.passwordTouched = true; },
  });
  const reveal = h("label", { class: "row small muted" },
    h("input", { type: "checkbox", onchange: (e) => { password.type = e.target.checked ? "text" : "password"; } }),
    "Show the password",
  );
  const address = h("input", { class: "field", placeholder: "192.168.1.20", "aria-label": "Host address" });
  const port = h("input", { class: "field", value: "6379", inputmode: "numeric", "aria-label": "Redis port" });
  connectEls = {
    password,
    laptop: h("div"),
    problem: h("div"),
    hosts: h("div", { class: "hostlist" }),
    hostButton: h("button", { class: "btn primary big block", text: "Host a cluster on this laptop", onclick: () => connect({ role: "host" }) }),
  };
  const point = (name, title, text) => h("li", null,
    h("span", { class: "badge" }, icon(name)),
    h("div", null, h("strong", { text: title }), h("span", { text })),
  );
  const step = (number, title, ...content) => h("div", { class: "step" },
    h("span", { class: "n", "aria-hidden": "true", text: String(number) }),
    h("div", null, h("h3", { text: title }), content),
  );
  return h("div", { class: "landing" },
    h("header", { class: "landing-top" }, brand(), h("span", { class: "grow" }), themeButton()),
    h("main", { class: "landing-grid" },
      h("section", { class: "pitch" },
        h("p", { class: "eyebrow", text: "Private AI on the laptops you already own" }),
        h("h1", null, "Pool your team's laptops into ", h("em", { text: "one AI cluster." })),
        h("p", { class: "lede", text: "Every laptop runs its own copy of an open model. A shared queue splits the work between them and pieces the answers back together, so a job that crawls on one machine finishes quickly on four." }),
        h("ul", { class: "points" },
          point("shield", "Nothing leaves the room", "Code, documents and pictures stay on your laptops. No cloud service sees them."),
          point("wallet", "No bill per question", "The model runs locally through Ollama, so asking more costs nothing more."),
          point("bolt", "Faster together", "Files are shared out as laptops become free, and a laptop that drops out has its work taken over."),
        ),
        icon("diagram"),
      ),
      h("section", { class: "card start", "aria-label": "Get started" },
        h("div", null,
          h("h2", { text: "Get started" }),
          h("p", { class: "sub", text: "Three steps, about a minute." }),
        ),
        connectEls.problem,
        step(1, "Check this laptop", connectEls.laptop),
        step(2, "Enter the cluster password",
          h("div", { class: "stack" },
            password,
            h("span", { class: "hint", text: "The Redis password. Every laptop in a cluster uses the same one." }),
          ),
          reveal,
        ),
        step(3, "Host a cluster, or join one",
          connectEls.hostButton,
          h("p", { class: "hint", text: "Hosting uses this laptop's Redis as the shared queue. Jobs are started from the host." }),
          h("div", { class: "or", text: "or join" }),
          connectEls.hosts,
          h("details", { class: "manual" },
            h("summary", { text: "Enter an address instead" }),
            h("div", { class: "grid" },
              h("label", { class: "stack" }, h("span", { text: "Address" }), address),
              h("label", { class: "stack" }, h("span", { text: "Port" }), port),
              h("button", {
                class: "btn", text: "Join",
                onclick: () => connect({ role: "member", name: "", address: address.value.trim(), port: Number(port.value) || 6379, follow: false }),
              }),
            ),
          ),
        ),
      ),
    ),
  );
}

async function setVisionOverride(value) {
  try {
    await api("/api/vision", { vision: value });
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

function drawConnect() {
  const d = ui.data;
  region(connectEls.problem, [ui.connectProblem, ui.serverDown, d.restore_problem], () => [
    ui.serverDown && h("div", { class: "problem" }, h("strong", { text: "The app on this laptop is not answering. " }), "Start it again with: python -m lapclusters"),
    !ui.connectProblem && d.restore_problem && h("div", { class: "problem" }, h("strong", { text: "Not reconnected. " }), d.restore_problem),
    ui.connectProblem && h("div", { class: "problem", role: "alert" }, h("strong", { text: "Could not connect. " }), ui.connectProblem),
  ]);
  region(connectEls.laptop, [d.me, d.ollama, d.models, d.model, d.provider, d.vision_override], () => h("dl", { class: "facts" },
    h("dt", { text: "Name" }), h("dd", { text: d.me }),
    h("dt", { text: d.provider === "llama_cpp" ? "llama.cpp" : "Ollama" }),
    h("dd", null, d.ollama
      ? status("done", `Running, ${plural(d.models.length, "model")} installed`)
      : status("failed", d.provider === "llama_cpp" ? "Not running. Start llama-server, then this updates by itself." : "Not running. Start Ollama, then this updates by itself.")),
    h("dt", { text: "Model" }), h("dd", null, modelPicker(d.me, d.model, d.models, d.ollama)),
    d.provider === "llama_cpp" && h("dt", { text: "Vision model?" }),
    d.provider === "llama_cpp" && h("dd", null,
      h("select", {
        class: "field", "aria-label": "Does this llama.cpp model support vision (images)?",
        onchange: (e) => setVisionOverride(e.target.value),
      },
        h("option", { value: "",     selected: !d.vision_override,              text: "Auto-detect" }),
        h("option", { value: "true",  selected: d.vision_override === "true",   text: "Yes — this model accepts images" }),
        h("option", { value: "false", selected: d.vision_override === "false",  text: "No — text only" }),
      ),
      d.provider === "llama_cpp" && !d.vision_override && h("span", {
        class: "hint",
        text: "llama.cpp didn't report vision capability. Choose manually if auto-detect is wrong.",
      }),
    ),
  ));
  // Show a remembered password that arrived after the box was built, but only
  // into an empty box: whatever is already in it, typed or filled in by a
  // password manager, is left alone.
  if (!ui.passwordTouched && ui.password && connectEls.password.value === "" && document.activeElement !== connectEls.password) {
    connectEls.password.value = ui.password;
  }
  connectEls.hostButton.disabled = ui.connecting;
  connectEls.hostButton.textContent = ui.connecting ? "Connecting…" : "Host a cluster on this laptop";
  region(connectEls.hosts, [ui.hosts, ui.connecting], () => {
    if (ui.hosts === null) return h("p", { class: "searching", text: "Looking for clusters on this network…" });
    if (!ui.hosts.length) return h("p", { class: "searching", text: "No cluster found yet. Ask the host to open LapClusters and choose Host. This list refreshes by itself." });
    return ui.hosts.map((host) => h("div", { class: "hostrow" },
      h("div", { class: "grow" },
        h("div", { class: "name", text: host.name }),
        h("div", { class: "muted mono", text: `${host.address}:${host.port}` }),
      ),
      host.is_me
        ? h("span", { class: "tag", text: "this laptop" })
        : h("button", {
          class: "btn primary", text: "Join", disabled: ui.connecting,
          onclick: () => connect({ role: "member", name: host.name, address: host.address, port: host.port, follow: true }),
        }),
    ));
  });
}

async function connect(body) {
  if (ui.connecting) return;
  if (body.role === "member" && !body.address) { ui.connectProblem = "Enter the host's address."; render(); return; }
  // Read the box itself, before anything is redrawn: a password manager or a
  // paste can fill it without the page being told, and then what was
  // remembered here would be stale. Spaces and line breaks picked up by
  // copying are dropped.
  const typed = connectEls && connectEls.password ? connectEls.password.value : (ui.password || "");
  ui.password = typed.trim();
  ui.passwordTouched = true;
  ui.connecting = true;
  ui.connectProblem = "";
  render();
  try {
    await api("/api/connect", { ...body, password: ui.password });
    await refresh();
  } catch (error) {
    ui.connectProblem = error.message;
  }
  ui.connecting = false;
  render();
}

// --- model picker, shared by both screens ----------------------------------

function modelPicker(laptop, current, models, canSwitch) {
  const pending = ui.pendingModel[laptop];
  const shown = pending ? pending.model : current;
  if (!canSwitch || !models || !models.length) return h("span", { class: "mono", text: shown || "unknown" });
  const options = models.includes(shown) ? models : [shown, ...models];
  return h("select", {
    class: "field mono", "aria-label": `Model for ${laptop}`, disabled: Boolean(pending),
    onchange: (e) => switchModel(laptop, e.target.value, e.target),
  }, options.map((name) => h("option", { value: name, selected: name === shown, text: name })));
}

async function switchModel(laptop, model, select) {
  const previous = ui.data.workers && ui.data.workers[laptop] ? ui.data.workers[laptop].model : ui.data.model;
  try {
    const reply = await api("/api/model", { worker: laptop, model });
    if (!reply.applied) {
      // Another laptop picks the change up at its next heartbeat.
      ui.pendingModel[laptop] = { model, until: Date.now() + 15000 };
      toast(`${laptop} will switch to ${model} after its current file.`);
    }
    await refresh();
  } catch (error) {
    select.value = previous;
    toast(error.message, true);
  }
}

function settlePendingModels() {
  for (const [laptop, pending] of Object.entries(ui.pendingModel)) {
    const info = (ui.data.workers || {})[laptop];
    if (!info || info.model === pending.model || Date.now() > pending.until) delete ui.pendingModel[laptop];
  }
}

// --- cluster screen --------------------------------------------------------

let els = null;
const TABS = [["review", "Work"], ["report", "Results"], ["history", "History"], ["activity", "Activity"]];
const KINDS = [
  { id: "review", label: "Review code", button: "Start review", source: true, question: false,
    hint: "Finds real defects in every source file of a repository. Long files are split into parts so nothing is skipped." },
  { id: "ask", label: "Ask about files", button: "Ask", source: true, question: true,
    placeholder: "What do you want to know? Leave empty for a summary of each file.",
    hint: "Asks one question of every file in a folder or repository: code, documents, PDFs and pictures. A picture is read together with the text that refers to it. The answers are then pieced into one." },
  { id: "prompt", label: "Prompt", button: "Ask", source: false, question: true,
    placeholder: "Ask anything.",
    hint: "One question, answered by whichever laptop is free." },
  { id: "code", label: "Write code", button: "Write it", source: false, question: true,
    placeholder: "Describe the program or function you need.",
    hint: "Describe what you need and get complete code back, with a note on how to run it." },
];

function buildCluster() {
  els = {
    brand: brand(),
    where: h("span", { class: "where" }),
    pills: h("span", { class: "pills" }),
    link: h("span"),
    banner: h("div"),
    laptops: h("aside", { class: "laptops", "aria-label": "Laptops in this cluster" }),
    tabs: h("div", { class: "tabs", role: "tablist" }),
    pane: h("div", { class: "pane" }),
    drawer: h("div"),
    review: null,
  };
  return [
    h("div", { class: "shell" },
      h("div", null,
        h("header", { class: "topbar" },
          els.brand,
          els.where,
          h("span", { class: "grow" }),
          els.pills,
          els.link,
          themeButton(),
          h("button", { class: "btn small", text: "Leave", onclick: leave }),
        ),
        els.banner,
      ),
      h("div", { class: "body" },
        els.laptops,
        h("div", { class: "main" }, els.tabs, els.pane),
      ),
    ),
    els.drawer,
  ];
}

async function leave() {
  const d = ui.data;
  const busy = d.job && ["running", "preparing"].includes(d.job.status);
  const warning = d.role === "host" && busy
    ? "A review is still running. Leaving stops this laptop hosting and reviewing; files already queued stay in Redis. Leave anyway?"
    : "Leave this cluster? This laptop stops reviewing files.";
  if (!confirm(warning)) return;
  try { await api("/api/disconnect", {}); } catch (error) { toast(error.message, true); }
  ui.hosts = null;
  await refresh();
  searchHosts();
}

function drawCluster() {
  const d = ui.data;
  settlePendingModels();

  region(els.where, [d.role, d.host, d.me], () => [
    h("span", null, h("b", { text: d.role === "host" ? d.me : (d.host ? d.host.name : "") }), "'s cluster"),
    h("span", { class: d.role === "host" ? "tag accent" : "tag", text: d.role === "host" ? "you host" : "member" }),
  ]);
  // The dots of the logo hop in turn while laptops are passing work around.
  els.brand.classList.toggle("busy", Boolean(d.job && d.job.running > 0));
  const everyone = Object.values(d.workers || {});
  const tally = [everyone.length, everyone.filter((w) => w.vision).length, d.job ? d.job.running : 0, d.job ? d.job.pending : 0];
  region(els.pills, tally, () => [
    h("span", { class: "pill" }, h("b", { text: String(tally[0]) }), ` ${tally[0] === 1 ? "laptop" : "laptops"} live`),
    h("span", { class: "pill" }, h("b", { text: String(tally[1]) }), " can read pictures"),
    (tally[2] + tally[3] > 0) && h("span", { class: "pill" }, h("b", { text: String(tally[2]) }), " working, ", h("b", { text: String(tally[3]) }), " queued"),
  ]);
  region(els.link, [d.connected, ui.serverDown], () =>
    ui.serverDown ? status("failed", "App stopped") : d.connected ? status("done", "Connected") : status("running", "Reconnecting"));
  region(els.banner, [ui.serverDown, d.connected, d.error, d.role, d.announcing], () => {
    if (ui.serverDown) return h("div", { class: "banner" }, h("strong", { text: "The app on this laptop stopped answering. " }), "Start it again with: python -m lapclusters");
    if (!d.connected) return h("div", { class: "banner" }, h("strong", { text: "Lost contact with the cluster. " }), d.error || "", " Trying again every few seconds; nothing needs restarting.");
    if (d.role === "host" && !d.announcing) return h("div", { class: "banner", text: "Another LapClusters process on this laptop is already announcing this cluster, so this one is not. That is fine if you started 'python -m lapclusters.host' on purpose." });
    return null;
  });

  drawLaptops();
  region(els.tabs, [ui.tab], () => TABS.map(([id, label]) => h("button", {
    class: "tab", role: "tab", "aria-selected": String(ui.tab === id), text: label,
    onclick: () => { ui.tab = id; render(); },
  })));
  if (els.pane.dataset.tab !== ui.tab) {
    els.pane.dataset.tab = ui.tab;
    els.pane.dataset.sig = "";
    els.pane.replaceChildren();
    els.review = null;
    els.report = null;
  }
  if (ui.tab === "review") drawReview();
  else if (ui.tab === "report") drawReport();
  else if (ui.tab === "history") drawHistory();
  else drawActivity();
  drawDrawer();
}

// --- laptops ---------------------------------------------------------------

function laptopList(d) {
  const workers = d.workers || {};
  const running = {};
  const done = {};
  for (const task of (d.job && d.job.tasks) || []) {
    if (task.status === "running" && task.worker) running[task.worker] = task;
    if (task.status === "done" && task.worker) done[task.worker] = (done[task.worker] || 0) + 1;
  }
  const names = new Set([d.me, ...Object.keys(workers)]);
  return [...names]
    .sort((a, b) => (a === d.me ? -1 : b === d.me ? 1 : a.localeCompare(b)))
    .map((name) => {
      const info = workers[name] || {};
      const isMe = name === d.me;
      return {
        name, isMe,
        isHost: d.host && (d.role === "host" ? isMe : name === d.host.name),
        live: name in workers,
        model: isMe ? d.model : info.model,
        models: isMe ? d.models : info.models,
        ollama: isMe ? d.ollama : info.ollama !== false,
        task: running[name] || null,
        done: done[name] || 0,
        state: isMe ? d.worker.state : (name in workers ? "running" : "stopped"),
        note: isMe ? d.worker.note : (info.problem || ""),
        sees: isMe ? d.vision : Boolean(info.vision),
        ready: info.ready !== false,
        // A worker from before tasks could be handed back reports no readiness.
        outdated: !isMe && name in workers && !("ready" in info),
        remote: Boolean(info.models),
      };
    });
}

function drawLaptops() {
  const d = ui.data;
  const list = laptopList(d);
  // Leave the list alone while one of its menus is open.
  if (els.laptops.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const signature = [list.map((l) => [l.name, l.live, l.model, l.models, l.ollama, l.task && l.task.id, l.done, l.state, l.note, l.isHost, l.sees, l.ready, l.outdated]), ui.pendingModel, d.role, d.connected];
  region(els.laptops, signature, () => [
    h("h2", null, h("span", { text: "Laptops" }), h("span", { text: `${list.filter((l) => l.live).length} live` })),
    list.map((laptop) => laptopCard(laptop, d)),
  ]);
}

function laptopCard(laptop, d) {
  let doing;
  if (laptop.task) {
    doing = [status("running", "Working on"), h("span", { class: "file mono", title: laptop.task.name, text: laptop.task.name }), h("span", { class: "muted" }, since(Number(laptop.task.started_at)))];
  } else if (laptop.isMe && laptop.state === "stopping") doing = status("running", "Finishing its last file, then stopping");
  else if (laptop.isMe && laptop.state === "stopped") doing = status("off", "Not reviewing");
  else if (!laptop.live) doing = status("off", "Starting…");
  else if (laptop.outdated) doing = status("failed", "Needs updating");
  else if (!laptop.ready) doing = status("failed", "Not taking work");
  else doing = status("pending", "Idle");

  const canSwitch = laptop.ollama && !laptop.outdated && (laptop.isMe || (d.role === "host" && laptop.remote && laptop.live));
  const kinds = ["laptop"];
  if (laptop.task) kinds.push("working");
  if (!laptop.live && !laptop.isMe) kinds.push("offline");

  // Two letters from the name, as a quick way to tell the cards apart.
  const initials = (laptop.name.replace(/[^A-Za-z0-9]/g, "").slice(0, 2) || "?").toUpperCase();
  return h("div", { class: kinds.join(" ") },
    h("div", { class: "who" },
      h("span", { class: laptop.isMe ? "avatar me" : "avatar", "aria-hidden": "true", text: initials }),
      h("div", { class: "grow" },
        h("div", { class: "name", title: laptop.name, text: laptop.name }),
        (laptop.isMe || laptop.isHost) && h("div", { class: "tags" },
          laptop.isMe && h("span", { class: "tag", text: "this laptop" }),
          laptop.isHost && h("span", { class: "tag accent", text: "host" }),
        ),
      ),
    ),
    h("div", { class: "doing" }, doing),
    h("div", { class: "meta" },
      h("div", { class: "line" }, h("span", { class: "k", text: "Model" }), h("span", { class: "grow" }, modelPicker(laptop.name, laptop.model, laptop.models, canSwitch))),
      (laptop.live || laptop.isMe) && h("div", { class: "line" }, h("span", { class: "k", text: "Reads" }), h("span", { text: laptop.sees ? "text and pictures" : "text only" })),
      (d.job && laptop.done > 0) && h("div", { class: "line" }, h("span", { class: "k", text: "Done" }), h("span", { text: plural(laptop.done, "task") })),
    ),
    laptop.outdated
      ? h("div", { class: "note", text: "This laptop runs an older LapClusters and will not be given work. On it, run 'git pull' and 'python -m pip install -r requirements.txt', then start the app again." })
      : laptop.note
      ? h("div", { class: "note", text: laptop.note })
      : !laptop.ollama && h("div", { class: "note", text: "Ollama is not running on that laptop." }),
    laptop.isMe && h("div", { class: "row" },
      laptop.state === "running"
        ? h("button", { class: "btn small", text: "Stop reviewing", onclick: () => setWorker("stop") })
        : h("button", { class: "btn small primary", text: laptop.state === "stopping" ? "Keep reviewing" : "Start reviewing", onclick: () => setWorker("start") }),
    ),
  );
}

async function setWorker(action) {
  try { await api("/api/worker", { action }); } catch (error) { toast(error.message, true); }
  await refresh();
}

// --- review tab ------------------------------------------------------------

function drawReview() {
  const d = ui.data;
  const job = d.job;
  if (!els.review) {
    const source = h("input", {
      class: "field mono", placeholder: "A folder on this laptop, or a git URL such as https://github.com/owner/repo",
      "aria-label": "Folder or git URL",
      onkeydown: (e) => { if (e.key === "Enter" && ui.kind === "review") startJob(); },
    });
    const question = h("textarea", {
      class: "field", rows: "2", "aria-label": "Question or request",
      // Enter starts; Shift+Enter makes a new line.
      onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); startJob(); } },
      // A screenshot or copied file can be pasted straight in.
      onpaste: (e) => { if (e.clipboardData && e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files); } },
    });
    const picker = h("input", {
      type: "file", multiple: true, class: "hidden", "aria-hidden": "true", tabindex: "-1",
      accept: "image/*,.pdf,.md,.txt,.csv,.json,.yaml,.yml,.html,.xml,.py,.js,.ts,.java,.go,.rs,.c,.cpp,.cs,.rb,.php,.sql,.sh",
      onchange: (e) => { attach(e.target.files); e.target.value = ""; },
    });
    els.review = {
      source, question, picker,
      kinds: h("div", { class: "kinds", role: "group", "aria-label": "Kind of job" }),
      hint: h("p", { class: "muted" }),
      attached: h("div"),
      attachButton: h("button", { class: "btn", text: "Attach files", onclick: () => picker.click() }),
      start: h("button", { class: "btn primary", onclick: startJob }),
      form: h("div", { class: "newjob" }),
      problem: h("div"),
      head: h("div", { class: "jobcard" }),
      answer: h("div"),
      files: h("div"),
    };
    const form = els.review.form;
    form.append(els.review.kinds, els.review.hint, source, els.review.attached, question, picker,
      h("div", { class: "row" }, els.review.start, els.review.attachButton));
    // Files can be dropped anywhere on the form.
    form.addEventListener("dragover", (e) => { e.preventDefault(); form.classList.add("dropping"); });
    form.addEventListener("dragleave", () => form.classList.remove("dropping"));
    form.addEventListener("drop", (e) => {
      e.preventDefault();
      form.classList.remove("dropping");
      if (e.dataTransfer && e.dataTransfer.files.length) attach(e.dataTransfer.files);
    });
    els.pane.replaceChildren(els.review.form, els.review.problem, els.review.head, els.review.answer, els.review.files);
  }
  const r = els.review;
  const busy = job && ["running", "preparing"].includes(job.status);
  const workers = Object.values(d.workers || {});
  const liveLaptops = workers.length;
  const kind = KINDS.find((k) => k.id === ui.kind);

  r.form.classList.toggle("hidden", d.role !== "host");
  region(r.kinds, [ui.kind], () => KINDS.map((k) => h("button", {
    class: "chip", "aria-pressed": String(k.id === ui.kind), text: k.label,
    onclick: () => { ui.kind = k.id; ui.reviewProblem = ""; render(); },
  })));
  const attaching = kind.id === "ask" && ui.attached.length > 0;
  r.hint.textContent = kind.hint;
  // With files attached, they are the source; the folder field steps aside.
  r.source.classList.toggle("hidden", !kind.source || attaching);
  r.attachButton.classList.toggle("hidden", kind.id !== "ask");
  r.question.classList.toggle("hidden", !kind.question);
  r.question.placeholder = kind.placeholder || "";
  region(r.attached, [kind.id, ui.attached.map((f) => [f.name, f.size])], () => {
    if (kind.id !== "ask") return null;
    if (!ui.attached.length) return h("p", { class: "muted small", text: "Or attach files: use the button, drop them here, or paste a screenshot into the question box." });
    return h("div", { class: "attached" },
      ui.attached.map((file, index) => h("span", { class: "filechip" },
        h("span", { class: "mono", text: file.name }),
        h("span", { class: "muted", text: sizeText(file.size) }),
        h("button", { class: "x", "aria-label": `Remove ${file.name}`, text: "×", onclick: () => { ui.attached.splice(index, 1); render(); } }),
      )),
      h("button", { class: "btn small quiet", text: "Remove all", onclick: () => { ui.attached = []; render(); } }),
    );
  });
  r.start.textContent = ui.startingReview ? (ui.uploadNote || "Starting…") : kind.button;
  r.start.disabled = ui.startingReview || busy || !d.connected;
  r.start.title = busy ? "Wait for the running job, or cancel it" : "";

  const nobodySees = job && job.waiting_for_sight > 0 && !workers.some((w) => w.vision);
  region(r.problem, [ui.reviewProblem, d.role, liveLaptops, busy, nobodySees && job.waiting_for_sight], () => [
    ui.reviewProblem && h("div", { class: "problem", role: "alert" }, h("strong", { text: "Could not start. " }), ui.reviewProblem),
    d.role === "host" && busy && liveLaptops === 0 && h("div", { class: "problem" }, h("strong", { text: "No laptop is working. " }), "Tasks stay queued until a laptop starts. Use Start reviewing on a laptop card."),
    nobodySees && h("div", { class: "problem" }, h("strong", { text: `${plural(job.waiting_for_sight, "task")} with pictures ${job.waiting_for_sight === 1 ? "is" : "are"} waiting. ` }), "No connected laptop is running a model that can see pictures. They start as soon as one switches to such a model or joins."),
  ]);

  if (!job) {
    region(r.head, ["none", d.role], () => h("div", { class: "empty" },
      h("strong", { text: "Nothing has been run yet" }),
      d.role === "host"
        ? "Choose a kind of job above. Work on files is split into one task per file, and whichever laptop is free takes the next one."
        : "The host starts jobs. When one starts, its tasks appear here and this laptop begins taking them."));
    region(r.answer, "none", () => null);
    region(r.files, "none", () => null);
    return;
  }

  const finished = job.done + job.failed;
  const following = !ui.job;
  const isReview = job.kind === "review";
  region(r.head, [job.id, job.kind, job.question, job.status, job.combining, job.error, job.total, job.done, job.failed, job.running, job.findings, job.laptops, job.duration, following, d.role, job.skipped.length], () => {
    const reading = isReview || job.kind === "ask" ? "Reading the files" : "Starting";
    const words = { preparing: reading, running: job.combining ? "Combining the answers" : "In progress", done: "Finished", cancelled: "Cancelled", failed: "Could not start" };
    const glyph = { preparing: "running", running: "running", done: "done", cancelled: "failed", failed: "failed" }[job.status] || "pending";
    const laptopCount = Object.keys(job.laptops).length;
    const label = KINDS.find((k) => k.id === job.kind);
    return h("div", { class: "jobhead" },
      h("div", { class: "row wrap" },
        status(glyph, words[job.status] || job.status),
        h("span", { class: "tag", text: label ? label.label : job.kind }),
        h("span", { class: "source grow", text: job.source }),
        !following && h("button", { class: "btn small", text: "Back to latest", onclick: () => { ui.job = ""; closeTask(); refresh(); } }),
        d.role === "host" && job.status === "running" && h("button", { class: "btn small", text: "Cancel", onclick: () => cancelReview(job.id) }),
        d.role === "host" && job.failed > 0 && job.status !== "preparing" && h("button", { class: "btn small", text: `Run ${plural(job.failed, "failed task")} again`, onclick: () => retryFailed(job.id) }),
        job.total > 0 && finished > 0 && h("a", { class: "btn small", href: `/api/reviews/${job.id}/report.md`, text: "Download report" }),
      ),
      job.question && h("p", { class: "question" }, h("span", { class: "muted", text: isReview ? "" : "Asked: " }), job.question),
      job.status === "failed" && h("div", { class: "problem" }, h("strong", { text: "This job did not start. " }), job.error),
      job.total > 0 && h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(job.total), "aria-valuenow": String(finished), "aria-label": "Tasks finished" },
        h("i", { class: "ok", style: `width:${(100 * job.done) / job.total}%` }),
        h("i", { class: "bad", style: `width:${(100 * job.failed) / job.total}%` }),
      ),
      job.total > 0 && h("div", { class: "stats" },
        stat(isReview ? "Files and parts" : "Tasks", [String(finished), h("small", { text: ` of ${job.total}` })]),
        stat("Time", job.duration !== null ? duration(job.duration) : (job.queued_at ? since(job.queued_at) : "–")),
        stat("Laptops used", String(laptopCount)),
        isReview && stat("Findings", counts(job.findings.high, job.findings.medium, job.findings.low)),
        job.failed > 0 && stat("Failed", String(job.failed)),
        job.skipped.length > 0 && stat("Skipped", String(job.skipped.length)),
      ),
    );
  });

  region(r.answer, [job.id, job.kind, job.status, job.answer, job.combining, job.answer_task], () => {
    if (isReview) return null;
    if (job.answer) {
      return h("section", { class: "answer", "aria-label": "Answer" },
        h("div", { class: "row" },
          h("h3", { class: "grow", text: "Answer" }),
          job.answer_task && h("button", { class: "btn small quiet", text: "How it was produced", onclick: () => openTask(job.answer_task) }),
          h("button", { class: "btn small", text: "Copy", onclick: () => copyText(job.answer) }),
        ),
        h("div", { class: "prose", text: job.answer }),
      );
    }
    if (job.status === "done" || job.status === "cancelled") {
      return h("div", { class: "problem" }, h("strong", { text: "No answer was produced. " }), "Open the failed tasks below to see why, then run them again.");
    }
    if (job.combining) return h("p", { class: "muted", text: "Every file has been answered. A laptop is now piecing the answers together." });
    return null;
  });

  const rows = job.tasks || [];
  region(r.files, [rows.map((t) => [t.id, t.status, t.worker, t.model, t.started_at, t.finished_at, t.count_high, t.count_medium, t.count_low, t.error, t.last_error]), ui.taskId, isReview], () => {
    if (!rows.length) return null;
    return h("table", null,
      h("thead", null, h("tr", null,
        h("th", { text: isReview ? "File" : "Task" }), h("th", { text: "Status" }), h("th", { text: "Laptop" }),
        h("th", { text: "Model" }), h("th", { class: "num", text: "Time" }), h("th", { text: isReview ? "Findings" : "Result" }),
      )),
      h("tbody", null, rows.map((task) => fileRow(task))),
    );
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied.");
  } catch {
    toast("This browser did not allow copying. Select the text and copy it by hand.", true);
  }
}

async function retryFailed(jobId) {
  try {
    const reply = await api(`/api/reviews/${jobId}/retry`, {});
    toast(`${plural(reply.retried, "task")} queued again.`);
  } catch (error) { toast(error.message, true); }
  await refresh();
}

function stat(label, value) {
  return h("div", { class: "stat" }, h("span", { class: "k", text: label }), h("span", { class: "v" }, value));
}

function fileRow(task) {
  let time = "–";
  if (task.status === "running" && task.started_at) time = since(Number(task.started_at));
  else if (task.started_at && task.finished_at) time = duration(Number(task.finished_at) - Number(task.started_at));
  let found = h("span", { class: "muted", text: "–" });
  if (task.status === "done") {
    found = (task.type || "review") === "review"
      ? counts(Number(task.count_high || 0), Number(task.count_medium || 0), Number(task.count_low || 0))
      : h("span", { class: "muted", text: task.type === "combine" ? "combined" : "answered" });
  } else if (task.status === "failed") found = h("span", { class: "muted", text: task.error || "" });
  else if (task.last_error) found = h("span", { class: "muted", text: `Handed back: ${task.last_error}` });
  else if (task.status === "pending" && task.lane === "vision") found = h("span", { class: "muted", text: "needs a model that sees pictures" });
  const open = () => openTask(task.id);
  return h("tr", {
    class: task.id === ui.taskId ? "pick selected" : "pick", tabindex: "0",
    onclick: open, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } },
  },
    h("td", { class: "file", text: task.name }),
    h("td", null, status(task.status)),
    h("td", { text: task.worker || "–" }),
    h("td", { class: "mono", text: task.model || "–" }),
    h("td", { class: "num" }, time),
    h("td", null, found),
  );
}

const MAX_ATTACH_BYTES = 30_000_000;
const MAX_ATTACH_FILES = 40;

function sizeText(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function attach(fileList) {
  const problems = [];
  for (const file of fileList) {
    // A pasted screenshot arrives with a generic name; make each one distinct.
    const pasted = file.name === "image.png" && file.lastModified && Date.now() - file.lastModified < 5000;
    const name = pasted ? `screenshot-${ui.attached.length + 1}.png` : file.name;
    if (file.size > MAX_ATTACH_BYTES) { problems.push(`${name} is larger than 30 MB`); continue; }
    if (ui.attached.length >= MAX_ATTACH_FILES) { problems.push(`only ${MAX_ATTACH_FILES} files can be attached at once`); break; }
    if (ui.attached.some((f) => f.name === name)) continue;
    ui.attached.push({ name, size: file.size, file });
  }
  if (ui.attached.length) ui.kind = "ask";  // attaching always means asking about those files
  ui.reviewProblem = problems.length ? `Not attached: ${problems.join("; ")}.` : "";
  render();
}

async function uploadAttached() {
  const { id } = await api("/api/uploads", {});
  for (const [index, item] of ui.attached.entries()) {
    ui.uploadNote = `Sending ${index + 1} of ${ui.attached.length}…`;
    render();
    const reply = await fetch(`/api/uploads/${id}/${encodeURIComponent(item.name)}`, {
      method: "PUT", headers: { "x-lapclusters": "1" }, body: item.file,
    });
    if (!reply.ok) {
      let message = `${item.name} could not be sent (${reply.status}).`;
      try { message = (await reply.json()).error || message; } catch { /* keep the default */ }
      throw new Error(message);
    }
  }
  return id;
}

async function startJob() {
  if (ui.startingReview || !els.review) return;
  const kind = KINDS.find((k) => k.id === ui.kind);
  const attaching = kind.id === "ask" && ui.attached.length > 0;
  const source = kind.source && !attaching ? els.review.source.value.trim() : "";
  const question = kind.question ? els.review.question.value.trim() : "";
  if (kind.source && !source && !attaching) { ui.reviewProblem = kind.id === "ask" ? "Enter a folder or a git URL, or attach files." : "Enter a folder or a git URL."; render(); return; }
  if (kind.question && !kind.source && !question) { ui.reviewProblem = "Type what you want to ask."; render(); return; }
  ui.startingReview = true;
  ui.uploadNote = "";
  ui.reviewProblem = "";
  render();
  try {
    const upload = attaching ? await uploadAttached() : "";
    await api("/api/reviews", { kind: kind.id, source, question, upload });
    ui.attached = [];
    els.review.question.value = "";
    ui.job = "";
    ui.findings = null;
    ui.findingsKey = "";
    closeTask();
  } catch (error) {
    ui.reviewProblem = error.message;
  }
  ui.startingReview = false;
  await refresh();
}

async function cancelReview(jobId) {
  if (!confirm("Cancel this job? Tasks not started yet are dropped. One already running finishes.")) return;
  try { await api(`/api/reviews/${jobId}/cancel`, {}); } catch (error) { toast(error.message, true); }
  await refresh();
}

// --- report tab ------------------------------------------------------------

function drawReport() {
  const job = ui.data.job;
  if (!job || !job.total) {
    els.report = null;
    region(els.pane, "report-empty", () => h("div", { class: "empty" }, h("strong", { text: "Nothing to report yet" }), "Findings appear here as files finish."));
    return;
  }
  const key = `${job.id}:${job.done}:${job.failed}`;
  if (ui.findingsKey !== key) {
    ui.findingsKey = key;
    api(`/api/reviews/${job.id}/findings`).then((data) => { if (ui.findingsKey === key) { ui.findings = data; render(); } }).catch(() => { ui.findingsKey = ""; });
  }
  if (job.kind !== "review") {
    els.report = null;
    const data = ui.findings;
    region(els.pane, ["answers", key, job.answer, data], () => [
      h("div", { class: "filters" },
        h("span", { class: "muted grow", text: job.question ? `Asked: ${job.question}` : "" }),
        h("a", { class: "btn small", href: `/api/reviews/${job.id}/report.md`, text: "Download report" }),
      ),
      job.answer
        ? h("section", { class: "answer" }, h("div", { class: "row" }, h("h3", { class: "grow", text: "Answer" }), h("button", { class: "btn small", text: "Copy", onclick: () => copyText(job.answer) })), h("div", { class: "prose", text: job.answer }))
        : h("p", { class: "muted", text: "The combined answer appears here when every file has been answered." }),
      data && data.notes.length > 0 && h("div", null,
        h("h3", { style: "font-size:13px;margin:18px 0 6px", text: `What each file contributed (${data.notes.length})` }),
        h("p", { class: "muted", style: "margin-bottom:8px", text: "The answer above was written from these notes alone. Open one to see its prompt and reply." }),
        h("table", null, h("tbody", null, data.notes.map((note) => h("tr", {
          class: "pick", tabindex: "0", onclick: () => openTask(note.task),
          onkeydown: (e) => { if (e.key === "Enter") openTask(note.task); },
        },
          h("td", { class: "file", text: note.file }),
          h("td", { text: note.text }),
          h("td", null, h("div", { text: note.worker }), h("div", { class: "muted mono", text: note.model })),
        )))),
      ),
      data && data.not_reviewed.length > 0 && h("div", null,
        h("h3", { style: "font-size:13px;margin:18px 0 6px", text: `Not included (${data.not_reviewed.length})` }),
        h("table", null, h("tbody", null, data.not_reviewed.map((item) => h("tr", null, h("td", { class: "file", text: item.file }), h("td", { class: "muted", text: item.reason }))))),
      ),
    ]);
    return;
  }
  if (!els.report || els.report.job !== job.id) {
    const search = h("input", { class: "field", placeholder: "Filter by file or wording", style: "max-width:280px", "aria-label": "Filter findings", oninput: (e) => { ui.filter.text = e.target.value.toLowerCase(); render(); } });
    search.value = ui.filter.text;
    const chips = h("span", { class: "row" });
    const list = h("div");
    els.pane.replaceChildren(h("div", { class: "filters" }, chips, search, h("span", { class: "grow" }), h("a", { class: "btn small", href: `/api/reviews/${job.id}/report.md`, text: "Download report" })), list);
    els.pane.dataset.sig = "report";
    els.report = { chips, list, job: job.id };
  }
  const data = ui.findings;
  region(els.report.chips, [ui.filter.high, ui.filter.medium, ui.filter.low], () => ["high", "medium", "low"].map((level) => h("button", {
    class: "chip", "aria-pressed": String(ui.filter[level]), onclick: () => { ui.filter[level] = !ui.filter[level]; render(); },
  }, h("span", { class: `sev ${level}`, text: level }))));
  region(els.report.list, [key, data, ui.filter], () => {
    if (!data) return h("p", { class: "muted", text: "Loading findings…" });
    const shown = data.findings.filter((f) => ui.filter[f.severity] !== false && (!ui.filter.text || `${f.file} ${f.message}`.toLowerCase().includes(ui.filter.text)));
    return [
      h("p", { class: "muted", text: `${shown.length} of ${plural(data.findings.length, "finding")} shown. A model can be wrong: treat each as a lead to check.` }),
      shown.length > 0 && h("table", null,
        h("thead", null, h("tr", null, h("th", { text: "Severity" }), h("th", { text: "Where" }), h("th", { text: "Problem" }), h("th", { text: "Reviewed by" }))),
        h("tbody", null, shown.map((f) => h("tr", {
          class: "pick", tabindex: "0", onclick: () => openTask(f.task),
          onkeydown: (e) => { if (e.key === "Enter") openTask(f.task); },
        },
          h("td", null, h("span", { class: `sev ${f.severity}`, text: f.severity })),
          h("td", { class: "file", text: f.line ? `${f.file}:${f.line}` : f.file }),
          h("td", { text: f.message }),
          h("td", null, h("div", { text: f.worker }), h("div", { class: "muted mono", text: f.model })),
        ))),
      ),
      data.not_reviewed.length > 0 && h("div", null,
        h("h3", { style: "font-size:13px;margin:18px 0 6px", text: `Not reviewed (${data.not_reviewed.length})` }),
        h("table", null, h("tbody", null, data.not_reviewed.map((item) => h("tr", null, h("td", { class: "file", text: item.file }), h("td", { class: "muted", text: item.reason }))))),
      ),
    ];
  });
}

// --- history and activity --------------------------------------------------

function drawHistory() {
  const jobs = ui.data.jobs || [];
  const isHost = ui.data.role === "host";
  region(els.pane, ["history", jobs.map((j) => [j.id, j.status, j.done, j.failed, j.duration]), ui.data.job && ui.data.job.id, isHost], () => {
    if (!jobs.length) return h("div", { class: "empty" }, h("strong", { text: "Nothing has been run yet" }), "Every job is kept here with its time and the laptops that took part, so runs can be compared.");
    const words = { preparing: "Reading", running: "In progress", done: "Finished", cancelled: "Cancelled", failed: "Did not start" };
    return [
      h("div", { class: "filters" },
        h("span", { class: "muted grow", text: "Run the same job with one laptop and then with several to compare the times." }),
        isHost && h("button", { class: "btn small", text: "Clear history", onclick: clearHistory }),
      ),
      h("table", null,
        h("thead", null, h("tr", null, h("th", { text: "Kind" }), h("th", { text: "What" }), h("th", { text: "Started" }), h("th", { text: "Status" }), h("th", { class: "num", text: "Tasks" }), h("th", { class: "num", text: "Laptops" }), h("th", { class: "num", text: "Time" }), h("th", { text: "Findings" }))),
        h("tbody", null, jobs.map((job) => {
          const open = () => { ui.job = job.id; ui.tab = "review"; ui.findings = null; ui.findingsKey = ""; closeTask(); refresh(); };
          const findings = job.findings || { high: 0, medium: 0, low: 0 };
          const kind = KINDS.find((k) => k.id === (job.kind || "review"));
          return h("tr", { class: ui.data.job && ui.data.job.id === job.id ? "pick selected" : "pick", tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
            h("td", { text: kind ? kind.label : job.kind }),
            h("td", { class: "file" }, job.source && h("div", { text: job.source }), job.question && h("div", { class: "muted sans", text: job.question })),
            h("td", { text: clock(job.created_at) }),
            h("td", { text: words[job.status] || job.status }),
            h("td", { class: "num", text: job.total ? `${job.done} of ${job.total}` : "–" }),
            h("td", { class: "num", text: String(Object.keys(job.laptops || {}).length) }),
            h("td", { class: "num", text: duration(job.duration) }),
            h("td", null, (job.kind || "review") === "review" ? counts(findings.high, findings.medium, findings.low) : h("span", { class: "muted", text: "–" })),
          );
        })),
      ),
    ];
  });
}

async function clearHistory() {
  if (!confirm("Delete every finished job and everything stored for it: results, prompts and replies? A job that is still running is kept. This cannot be undone.")) return;
  try {
    const reply = await api("/api/history/clear", {});
    toast(`${plural(reply.deleted, "job")} deleted.`);
    ui.job = "";
    ui.findings = null;
    ui.findingsKey = "";
    closeTask();
  } catch (error) { toast(error.message, true); }
  await refresh();
}

function drawActivity() {
  const events = ui.data.events || [];
  region(els.pane, ["activity", events.length && events[0].id], () => {
    if (!events.length) return h("div", { class: "empty" }, h("strong", { text: "Nothing has happened yet" }), "Laptops joining and leaving, model changes, takeovers and reviews are listed here as they happen.");
    return h("div", { class: "feed" }, events.map((event) => h("div", { class: "item" }, h("time", { text: clock(Number(event.ts)) }), h("span", { text: event.text }))));
  });
}

// --- file detail -----------------------------------------------------------

let taskTimer = null;
let outputTimer = null;

function openTask(taskId) {
  closeTask();
  ui.taskId = taskId;
  loadTask();
  taskTimer = setInterval(loadTask, 1500);
  outputTimer = setInterval(loadOutput, 400);
  render();
}

function closeTask() {
  clearInterval(taskTimer);
  clearInterval(outputTimer);
  taskTimer = outputTimer = null;
  ui.taskId = null;
  ui.task = null;
  ui.taskTab = null;
  ui.output = { text: "", next: 0 };
}

async function loadTask() {
  const id = ui.taskId;
  if (!id) return;
  try {
    const task = await api(`/api/tasks/${id}`);
    if (ui.taskId !== id) return;
    ui.task = task;
    if (task.status === "done" || task.status === "failed") {
      clearInterval(taskTimer);
      clearInterval(outputTimer);
      taskTimer = outputTimer = null;
    }
    render();
  } catch { /* shown as loading; the next try may succeed */ }
}

async function loadOutput() {
  const id = ui.taskId;
  if (!id || !ui.task || ui.task.status !== "running") return;
  try {
    const reply = await api(`/api/tasks/${id}/output?offset=${ui.output.next}`);
    if (ui.taskId !== id) return;
    ui.output = { text: reply.restarted ? reply.text : ui.output.text + reply.text, next: reply.next };
    if (reply.text || reply.restarted) render();
  } catch { /* try again on the next tick */ }
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && ui.taskId) { closeTask(); render(); } });

function drawDrawer() {
  if (!ui.taskId) { region(els.drawer, "closed", () => null); els.drawer.className = ""; return; }
  els.drawer.className = "drawer";
  els.drawer.setAttribute("role", "dialog");
  els.drawer.setAttribute("aria-label", "File detail");
  const task = ui.task;
  if (!task) { region(els.drawer, ["loading", ui.taskId], () => h("header", null, h("p", { class: "muted", text: "Loading…" }))); return; }

  const finished = task.status === "done" || task.status === "failed";
  const tabs = [["findings", task.type === "review" ? "Findings" : "Answer"], ["reply", finished ? "Raw reply" : "Live reply"], ["prompt", "Prompt"], ["timeline", "Timeline"]];
  const tab = ui.taskTab || (task.status === "done" ? "findings" : task.status === "failed" ? "timeline" : "reply");

  if (!els.drawer.querySelector(".content")) {
    els.drawerHead = h("header");
    els.drawerTabs = h("div", { class: "tabs", role: "tablist" });
    els.drawerBody = h("div", { class: "content" });
    els.drawer.dataset.sig = "";
    els.drawer.replaceChildren(els.drawerHead, els.drawerTabs, els.drawerBody);
  }
  region(els.drawerHead, [task.id, task.status, task.worker, task.model, task.started_at], () => [
    h("div", { class: "row" }, h("h3", { class: "grow", text: task.name || "Task" }), h("button", { class: "btn small", text: "Close", "aria-label": "Close file detail", onclick: () => { closeTask(); render(); } })),
    h("div", { class: "row wrap" },
      status(task.status),
      task.worker && h("span", { text: task.worker }),
      task.model && h("span", { class: "mono muted", text: task.model }),
      task.status === "running" && task.started_at && h("span", { class: "muted" }, since(task.started_at)),
    ),
  ]);
  region(els.drawerTabs, [tab, finished], () => tabs.map(([id, label]) => h("button", {
    class: "tab", role: "tab", "aria-selected": String(tab === id), text: label, onclick: () => { ui.taskTab = id; render(); },
  })));

  if (tab === "reply" && !finished) {
    // Keep the newest text in view unless the reader has scrolled back up.
    const body = els.drawerBody;
    const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    region(body, ["live", task.status, ui.output.text.length], () => liveReply(task));
    if (atEnd) body.scrollTop = body.scrollHeight;
    return;
  }
  region(els.drawerBody, [tab, task.id, task.status, task.finished_at], () => drawerBody(tab, task));
}

function liveReply(task) {
  if (task.status === "pending") return h("p", { class: "muted", text: "Waiting for a laptop to become free. The reply appears here as soon as one starts on this file." });
  if (!ui.output.text) {
    return [
      h("div", { class: "live" }, status("running", `${task.worker} is reading the file`)),
      h("p", { class: "muted", text: "The model reads the whole file before it writes anything. Its reply then appears here as it is written. A laptop running an older worker shows the reply only once it has finished." }),
    ];
  }
  return [
    h("div", { class: "live" }, status("running", `${task.worker} is writing`), h("span", { text: `· ${ui.output.text.length} characters so far` })),
    h("pre", { class: "code", text: ui.output.text }),
  ];
}

function drawerBody(tab, task) {
  if (tab === "findings") {
    if (task.status === "failed") return h("div", { class: "problem" }, h("strong", { text: "This task did not finish. " }), task.error);
    if (task.status !== "done") return h("p", { class: "muted", text: "The result appears here when this task finishes." });
    if (!task.findings) {
      return [
        h("div", { class: "row", style: "margin-bottom:10px" }, h("span", { class: "grow" }), h("button", { class: "btn small", text: "Copy", onclick: () => copyText(task.result) })),
        h("div", { class: "prose", text: task.result || "(empty reply)" }),
      ];
    }
    if (!task.findings.length) return h("p", { text: "The model reported no problems in this file." });
    return task.findings.map((f) => h("div", { class: "finding" },
      h("div", { class: "row" }, h("span", { class: `sev ${f.severity}`, text: f.severity }), h("span", { class: "where mono", text: f.line ? `line ${f.line}` : "no line given" })),
      h("div", { text: f.message }),
    ));
  }
  if (tab === "reply") {
    const text = task.raw || task.result || ui.output.text;
    return text
      ? [h("p", { class: "muted", style: "margin-bottom:10px", text: "Exactly what the model wrote, before it was parsed." }), h("pre", { class: "code", text })]
      : h("p", { class: "muted", text: task.status === "failed" ? "The model produced no reply before this file failed." : "No reply was stored for this file." });
  }
  if (tab === "prompt") {
    return task.prompt
      ? [h("p", { class: "muted", style: "margin-bottom:10px", text: "Exactly what was sent to the model, including the file with numbered lines." }), h("pre", { class: "code", text: task.prompt })]
      : h("p", { class: "muted", text: "The prompt is stored when a laptop starts on the file." });
  }
  const steps = [];
  if (task.queued_at) steps.push([task.queued_at, `Queued by the host.${task.needs_sight ? " It includes a picture, so only a laptop whose model can see may take it." : ""}`]);
  if (task.attempts > 0) steps.push([null, `Handed back to the queue ${task.attempts === 1 ? "once" : `${task.attempts} times`} by a laptop that could not run it. Last reason: ${task.last_error || "not recorded"}.`]);
  if (task.started_at) {
    const waited = task.queued_at ? ` after waiting ${duration(task.started_at - task.queued_at)}` : "";
    const from = task.taken_over_from ? ` It was taken over from ${task.taken_over_from}, which stopped responding.` : "";
    steps.push([task.started_at, `Started on ${task.worker || "a laptop"}${task.model ? ` with ${task.model}` : ""}${waited}.${from}`]);
  }
  if (task.finished_at) {
    const took = task.started_at ? ` after ${duration(task.finished_at - task.started_at)}` : "";
    const outcome = task.type === "review" ? ` with ${plural((task.findings || []).length, "finding")}` : "";
    steps.push([task.finished_at, task.status === "done"
      ? `Finished${took}${outcome}.`
      : `Failed${took}: ${task.error}`]);
  }
  return h("ol", { class: "timeline" }, steps.map(([stamp, text]) => h("li", null, h("time", { text: stamp ? clock(stamp) : "" }), h("span", { text }))));
}

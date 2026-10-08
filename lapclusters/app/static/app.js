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
    if (ui.password === null) ui.password = data.saved_password || "";
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

// --- connect screen --------------------------------------------------------

let connectEls = null;

function buildConnect() {
  const password = h("input", {
    class: "field", type: "password", autocomplete: "off", placeholder: "Leave empty if Redis has none",
    value: ui.password || "", oninput: (e) => { ui.password = e.target.value; },
  });
  const address = h("input", { class: "field", placeholder: "192.168.1.20", "aria-label": "Host address" });
  const port = h("input", { class: "field", value: "6379", inputmode: "numeric", "aria-label": "Redis port" });
  connectEls = {
    laptop: h("div"),
    problem: h("div"),
    hosts: h("div", { class: "hostlist" }),
    hostButton: h("button", { class: "btn primary", text: "Host a cluster on this laptop", onclick: () => connect({ role: "host" }) }),
  };
  return h("div", { class: "connect" },
    h("div", null,
      h("h1", { text: "LapClusters" }),
      h("p", { class: "lede", text: "Pool the laptops on this network into one private AI cluster. Every laptop runs its own model; nothing is sent to a cloud service." }),
    ),
    connectEls.problem,
    h("div", { class: "panel" },
      h("h2", { text: "This laptop" }),
      connectEls.laptop,
    ),
    h("div", { class: "panel" },
      h("label", { class: "stack" },
        h("span", { text: "Cluster password" }),
        password,
        "The Redis password. Every laptop in a cluster uses the same one.",
      ),
    ),
    h("div", { class: "choices" },
      h("div", { class: "panel" },
        h("h2", { text: "Host" }),
        h("p", { class: "hint", text: "Use this laptop's Redis as the shared queue. Other laptops on the network will see it and can join. Reviews are started from the host." }),
        connectEls.hostButton,
      ),
      h("div", { class: "panel" },
        h("h2", { text: "Join" }),
        h("p", { class: "hint", text: "Clusters found on this network appear here." }),
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
  );
}

function drawConnect() {
  const d = ui.data;
  region(connectEls.problem, [ui.connectProblem, ui.serverDown], () => [
    ui.serverDown && h("div", { class: "problem" }, h("strong", { text: "The app on this laptop is not answering. " }), "Start it again with: python -m lapclusters"),
    ui.connectProblem && h("div", { class: "problem", role: "alert" }, h("strong", { text: "Could not connect. " }), ui.connectProblem),
  ]);
  region(connectEls.laptop, [d.me, d.ollama, d.models, d.model], () => h("dl", { class: "facts" },
    h("dt", { text: "Name" }), h("dd", { text: d.me }),
    h("dt", { text: "Ollama" }), h("dd", null, d.ollama
      ? status("done", `Running, ${plural(d.models.length, "model")} installed`)
      : status("failed", "Not running. Start Ollama, then this updates by itself.")),
    h("dt", { text: "Model" }), h("dd", null, modelPicker(d.me, d.model, d.models, d.ollama)),
  ));
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
  ui.connecting = true;
  ui.connectProblem = "";
  render();
  try {
    await api("/api/connect", { ...body, password: ui.password || "" });
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
const TABS = [["review", "Review"], ["report", "Report"], ["history", "History"], ["activity", "Activity"]];

function buildCluster() {
  els = {
    where: h("span", { class: "where" }),
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
          h("span", { class: "brand", text: "LapClusters" }),
          els.where,
          h("span", { class: "grow" }),
          els.link,
          h("button", { class: "btn quiet small", text: "Theme", title: "Switch between light and dark", onclick: () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark") }),
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

  region(els.where, [d.role, d.host, d.announcing], () => [
    h("span", { text: d.role === "host" ? "Hosting on this laptop" : `Joined ${d.host ? d.host.name : ""}` }),
    " ",
    h("span", { class: d.role === "host" ? "tag accent" : "tag", text: d.role === "host" ? "host" : "member" }),
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
        note: isMe ? d.worker.note : "",
        remote: Boolean(info.models),
      };
    });
}

function drawLaptops() {
  const d = ui.data;
  const list = laptopList(d);
  // Leave the list alone while one of its menus is open.
  if (els.laptops.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const signature = [list.map((l) => [l.name, l.live, l.model, l.models, l.ollama, l.task && l.task.id, l.done, l.state, l.note, l.isHost]), ui.pendingModel, d.role, d.connected];
  region(els.laptops, signature, () => [
    h("h2", null, h("span", { text: "Laptops" }), h("span", { text: `${list.filter((l) => l.live).length} live` })),
    list.map((laptop) => laptopCard(laptop, d)),
  ]);
}

function laptopCard(laptop, d) {
  let doing;
  if (laptop.task) {
    doing = [status("running", ""), h("span", { class: "file mono", title: laptop.task.name, text: laptop.task.name }), h("span", { class: "muted" }, since(Number(laptop.task.started_at)))];
  } else if (laptop.isMe && laptop.state === "stopping") doing = status("running", "Finishing its last file, then stopping");
  else if (laptop.isMe && laptop.state === "stopped") doing = status("off", "Not reviewing");
  else if (!laptop.live) doing = status("off", "Starting…");
  else doing = status("pending", "Idle");

  const canSwitch = laptop.ollama && (laptop.isMe || (d.role === "host" && laptop.remote && laptop.live));
  const kinds = ["laptop"];
  if (laptop.task) kinds.push("working");
  if (!laptop.live && !laptop.isMe) kinds.push("offline");

  return h("div", { class: kinds.join(" ") },
    h("div", { class: "row" },
      h("span", { class: "name grow", title: laptop.name, text: laptop.name }),
      laptop.isMe && h("span", { class: "tag", text: "this laptop" }),
      laptop.isHost && h("span", { class: "tag accent", text: "host" }),
    ),
    h("div", { class: "doing" }, doing),
    h("div", { class: "line" }, h("span", { class: "k", text: "Model" }), h("span", { class: "grow" }, modelPicker(laptop.name, laptop.model, laptop.models, canSwitch))),
    (d.job && laptop.done > 0) && h("div", { class: "line" }, h("span", { class: "k", text: "Done" }), h("span", { text: plural(laptop.done, "file") })),
    !laptop.ollama && h("div", { class: "note", text: laptop.isMe ? "Ollama is not running on this laptop, so files it takes will fail." : "Ollama is not running on that laptop." }),
    laptop.note && h("div", { class: "note", text: laptop.note }),
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
      "aria-label": "Folder or git URL to review",
      onkeydown: (e) => { if (e.key === "Enter") startReview(source); },
    });
    els.review = {
      source,
      start: h("button", { class: "btn primary", text: "Start review", onclick: () => startReview(source) }),
      form: h("div"),
      problem: h("div"),
      head: h("div"),
      files: h("div"),
    };
    els.review.form.append(h("div", { class: "newreview" }, els.review.source, els.review.start));
    els.pane.replaceChildren(els.review.form, els.review.problem, els.review.head, els.review.files);
  }
  const r = els.review;
  const busy = job && ["running", "preparing"].includes(job.status);
  const liveLaptops = Object.keys(d.workers || {}).length;

  r.form.classList.toggle("hidden", d.role !== "host");
  r.start.disabled = ui.startingReview || busy || !d.connected;
  r.start.title = busy ? "Wait for the running review, or cancel it" : "";
  region(r.problem, [ui.reviewProblem, d.role, liveLaptops, busy], () => [
    ui.reviewProblem && h("div", { class: "problem", role: "alert" }, h("strong", { text: "Could not start. " }), ui.reviewProblem),
    d.role === "host" && busy && liveLaptops === 0 && h("div", { class: "problem" }, h("strong", { text: "No laptop is reviewing. " }), "Files stay queued until a laptop starts reviewing. Use Start reviewing on a laptop card."),
  ]);

  if (!job) {
    region(r.head, ["none", d.role], () => h("div", { class: "empty" },
      h("strong", { text: "No review yet" }),
      d.role === "host"
        ? "Paste a folder path or a git URL above. Each source file becomes one task, and whichever laptop is free takes the next one."
        : "The host starts reviews. When one starts, its files appear here and this laptop begins taking them."));
    region(r.files, "none", () => null);
    return;
  }

  const finished = job.done + job.failed;
  const following = !ui.job;
  region(r.head, [job.id, job.status, job.error, job.total, job.done, job.failed, job.running, job.findings, job.laptops, job.duration, following, d.role, job.skipped.length], () => {
    const words = { preparing: "Reading the repository", running: "In progress", done: "Finished", cancelled: "Cancelled", failed: "Could not start" };
    const kind = { preparing: "running", running: "running", done: "done", cancelled: "failed", failed: "failed" }[job.status] || "pending";
    const laptopCount = Object.keys(job.laptops).length;
    return h("div", { class: "jobhead" },
      h("div", { class: "row wrap" },
        status(kind, words[job.status] || job.status),
        h("span", { class: "source grow", text: job.source }),
        !following && h("button", { class: "btn small", text: "Back to latest", onclick: () => { ui.job = ""; closeTask(); refresh(); } }),
        d.role === "host" && job.status === "running" && h("button", { class: "btn small", text: "Cancel review", onclick: () => cancelReview(job.id) }),
        job.total > 0 && finished > 0 && h("a", { class: "btn small", href: `/api/reviews/${job.id}/report.md`, text: "Download report" }),
      ),
      job.status === "failed" && h("div", { class: "problem" }, h("strong", { text: "This review did not start. " }), job.error),
      job.total > 0 && h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(job.total), "aria-valuenow": String(finished), "aria-label": "Files finished" },
        h("i", { class: "ok", style: `width:${(100 * job.done) / job.total}%` }),
        h("i", { class: "bad", style: `width:${(100 * job.failed) / job.total}%` }),
      ),
      job.total > 0 && h("div", { class: "stats" },
        stat("Files", [String(finished), h("small", { text: ` of ${job.total}` })]),
        stat("Time", job.duration !== null ? duration(job.duration) : (job.queued_at ? since(job.queued_at) : "–")),
        stat("Laptops used", String(laptopCount)),
        stat("Findings", counts(job.findings.high, job.findings.medium, job.findings.low)),
        job.failed > 0 && stat("Failed", String(job.failed)),
        job.skipped.length > 0 && stat("Skipped", String(job.skipped.length)),
      ),
    );
  });

  const rows = job.tasks || [];
  region(r.files, [rows.map((t) => [t.id, t.status, t.worker, t.model, t.started_at, t.finished_at, t.count_high, t.count_medium, t.count_low, t.error]), ui.taskId], () => {
    if (!rows.length) return null;
    return h("table", null,
      h("thead", null, h("tr", null,
        h("th", { text: "File" }), h("th", { text: "Status" }), h("th", { text: "Laptop" }),
        h("th", { text: "Model" }), h("th", { class: "num", text: "Time" }), h("th", { text: "Findings" }),
      )),
      h("tbody", null, rows.map((task) => fileRow(task))),
    );
  });
}

function stat(label, value) {
  return h("div", { class: "stat" }, h("span", { class: "k", text: label }), h("span", { class: "v" }, value));
}

function fileRow(task) {
  let time = "–";
  if (task.status === "running" && task.started_at) time = since(Number(task.started_at));
  else if (task.started_at && task.finished_at) time = duration(Number(task.finished_at) - Number(task.started_at));
  let found = h("span", { class: "muted", text: "–" });
  if (task.status === "done") found = counts(Number(task.count_high || 0), Number(task.count_medium || 0), Number(task.count_low || 0));
  else if (task.status === "failed") found = h("span", { class: "muted", text: task.error || "" });
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

async function startReview(input) {
  const source = input.value.trim();
  if (!source) { ui.reviewProblem = "Enter a folder or a git URL."; render(); return; }
  ui.startingReview = true;
  ui.reviewProblem = "";
  render();
  try {
    await api("/api/reviews", { source });
    input.value = "";
    ui.job = "";
    closeTask();
  } catch (error) {
    ui.reviewProblem = error.message;
  }
  ui.startingReview = false;
  await refresh();
}

async function cancelReview(jobId) {
  if (!confirm("Cancel this review? Files not started yet are dropped. A file already being reviewed finishes.")) return;
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
  region(els.pane, ["history", jobs.map((j) => [j.id, j.status, j.done, j.failed, j.duration]), ui.data.job && ui.data.job.id], () => {
    if (!jobs.length) return h("div", { class: "empty" }, h("strong", { text: "No reviews yet" }), "Every review is kept here with its time and the laptops that took part, so runs can be compared.");
    const words = { preparing: "Reading", running: "In progress", done: "Finished", cancelled: "Cancelled", failed: "Did not start" };
    return h("table", null,
      h("thead", null, h("tr", null, h("th", { text: "Source" }), h("th", { text: "Started" }), h("th", { text: "Status" }), h("th", { class: "num", text: "Files" }), h("th", { class: "num", text: "Laptops" }), h("th", { class: "num", text: "Time" }), h("th", { text: "Findings" }))),
      h("tbody", null, jobs.map((job) => {
        const open = () => { ui.job = job.id; ui.tab = "review"; ui.findings = null; ui.findingsKey = ""; closeTask(); refresh(); };
        const findings = job.findings || { high: 0, medium: 0, low: 0 };
        return h("tr", { class: ui.data.job && ui.data.job.id === job.id ? "pick selected" : "pick", tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
          h("td", { class: "file", text: job.source }),
          h("td", { text: clock(job.created_at) }),
          h("td", { text: words[job.status] || job.status }),
          h("td", { class: "num", text: job.total ? `${job.done} of ${job.total}` : "–" }),
          h("td", { class: "num", text: String(Object.keys(job.laptops || {}).length) }),
          h("td", { class: "num", text: duration(job.duration) }),
          h("td", null, counts(findings.high, findings.medium, findings.low)),
        );
      })),
    );
  });
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
  const tabs = [["findings", "Findings"], ["reply", finished ? "Raw reply" : "Live reply"], ["prompt", "Prompt"], ["timeline", "Timeline"]];
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
    if (task.status === "failed") return h("div", { class: "problem" }, h("strong", { text: "This file was not reviewed. " }), task.error);
    if (task.status !== "done") return h("p", { class: "muted", text: "Findings appear when the review of this file finishes." });
    if (!task.findings) return h("pre", { class: "code", text: task.result || "(empty reply)" });
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
  if (task.queued_at) steps.push([task.queued_at, "Queued by the host."]);
  if (task.started_at) {
    const waited = task.queued_at ? ` after waiting ${duration(task.started_at - task.queued_at)}` : "";
    const from = task.taken_over_from ? ` It was taken over from ${task.taken_over_from}, which stopped responding.` : "";
    steps.push([task.started_at, `Started on ${task.worker || "a laptop"}${task.model ? ` with ${task.model}` : ""}${waited}.${from}`]);
  }
  if (task.finished_at) {
    const took = task.started_at ? ` after ${duration(task.finished_at - task.started_at)}` : "";
    steps.push([task.finished_at, task.status === "done"
      ? `Finished${took} with ${plural((task.findings || []).length, "finding")}.`
      : `Failed${took}: ${task.error}`]);
  }
  return h("ol", { class: "timeline" }, steps.map(([stamp, text]) => h("li", null, h("time", { text: clock(stamp) }), h("span", { text }))));
}

"use strict";

const byId = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let info = null;
let status = null;
let busy = false;
// Each progress flow polls until a newer flow, or a dismiss, takes over.
let flow = 0;

const UNIT_NAMES = {
  "kalinka.service": "Server",
  "kalinka-renderer.service": "Renderer",
  "kalinka-supervisor.service": "Supervisor",
  "kalinka-upgrade.service": "Upgrade",
  "kalinka-renderer-upgrade.service": "Renderer upgrade",
  "kalinka-reinstall.service": "Reinstall",
};

const STATES = {
  active: ["Running", "ok"],
  activating: ["Starting", "wait"],
  reloading: ["Reloading", "wait"],
  deactivating: ["Stopping", "wait"],
  failed: ["Failed", "bad"],
  inactive: ["Stopped", "off"],
};

const SETUP = { running: "On", waiting: "Waiting", off: "Off" };

const ACTIONS = {
  restart_core: {
    icon: "i-restart",
    title: "Restart server?",
    body: "Kalinka's server starts again. Playback stops for a moment, and the app reconnects by itself.",
    go: "Restart",
    follow: followRestart,
  },
  reboot: {
    icon: "i-reboot",
    title: "Restart the box?",
    body: "The whole player restarts. Playback stops, and the box is back in a minute or two.",
    go: "Restart",
    follow: followReboot,
  },
  poweroff: {
    icon: "i-power",
    danger: true,
    title: "Power off the box?",
    body: "The player shuts down safely. To start it again, unplug it and plug it back in.",
    go: "Power off",
    follow: followPowerOff,
  },
  reinstall: {
    icon: "i-reinstall",
    danger: true,
    title: "Reinstall Kalinka?",
    body: "Downloads the installer from kalinkaplayer.com, installs the server, its plugins and the renderer again, and rebuilds the server's Python environment. Settings and music stay. It takes several minutes, and playback stops until it finishes.",
    go: "Reinstall",
    follow: followReinstall,
  },
};

function unitName(unit) {
  if (UNIT_NAMES[unit]) return UNIT_NAMES[unit];
  const wifi = /^kalinka-wifi@(.+)\.service$/.exec(unit);
  if (wifi) return `Wi-Fi (${wifi[1]})`;
  if (unit.startsWith("kalinka-journal-reader@")) return "Log reader";
  return unit;
}

function stateLook(state) {
  return STATES[state] || ["Unknown", "off"];
}

function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function percent(p) {
  if (p == null) return "–";
  return `${p < 10 ? p.toFixed(1) : Math.round(p)}%`;
}

function duration(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} day${d === 1 ? "" : "s"} ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
}

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

async function getLog() {
  const response = await fetch("/v1/reinstall/log", { cache: "no-store" });
  return response.ok ? response.text() : "";
}

async function post(action) {
  const body = info && info.server_id ? { server_id: info.server_id } : {};
  const response = await fetch(`/v1/actions/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 202) return;
  let message = "The supervisor did not accept the request.";
  try {
    message = (await response.json()).detail.message;
  } catch (_) {
    // The fixed message above stands.
  }
  throw new Error(message);
}

function notice(kind, text, button) {
  const el = document.createElement("div");
  el.className = `notice notice-${kind}`;
  const span = document.createElement("span");
  span.textContent = text;
  el.append(span);
  if (button) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "button neutral";
    b.textContent = button.label;
    b.addEventListener("click", button.onClick);
    el.append(b);
  }
  return el;
}

function renderNotices() {
  const list = byId("notices");
  list.replaceChildren();
  if (!status) return;
  const s = status;
  if (s.pending === "poweroff") list.append(notice("wait", "The box is powering off."));
  if (s.pending === "reboot") list.append(notice("wait", "The box is restarting."));
  if (s.reinstall.state === "running") {
    list.append(notice("wait", "Kalinka is being reinstalled.", { label: "Show progress", onClick: () => followReinstall(null) }));
  } else if (s.upgrading) {
    list.append(notice("wait", "Packages are being installed. The controls wait until that finishes."));
  }
  if (s.reinstall.state === "failed") {
    const at = s.reinstall.finished_at ? ` on ${new Date(s.reinstall.finished_at).toLocaleString()}` : "";
    list.append(notice("error", `The last reinstall failed${at}.`, { label: "Show log", onClick: showFailedLog }));
  }
  if (s.core === "failed") {
    list.append(notice("error", "Kalinka's server stopped after an error. Restarting it often helps; if it keeps failing, reinstall."));
  }
}

function renderStatus() {
  const [label, look] = status ? stateLook(status.core) : ["Unknown", "off"];
  byId("core-pill").className = `pill ${look}`;
  byId("core-label").textContent = status ? `Server ${label.toLowerCase()}` : "Checking the server…";
  const open = byId("open-core");
  open.hidden = !status || status.core !== "active";
  if (status) open.href = `${location.protocol}//${location.hostname}:${status.core_port}/`;
  renderNotices();
  const locked = !status || status.upgrading || status.pending != null || busy;
  for (const button of document.querySelectorAll("[data-action]")) {
    const offered = info && info.actions.includes(button.dataset.action);
    button.disabled = locked || !offered;
  }
}

function fact(list, label, value) {
  if (value == null || value === "") return;
  const node = byId("fact").content.firstElementChild.cloneNode(true);
  node.querySelector("dt").textContent = label;
  node.querySelector("dd").textContent = value;
  list.append(node);
}

function meter(id, used, total) {
  const known = total > 0;
  byId(`${id}-text`).textContent = known ? `${bytes(used)} of ${bytes(total)}` : "–";
  byId(`${id}-fill`).style.width = known ? `${Math.min(100, (used / total) * 100)}%` : "0";
}

function versions(id, items, label) {
  const list = byId(id);
  list.replaceChildren();
  for (const item of items) fact(list, label(item.name), item.version);
  byId(`${id}-empty`).hidden = items.length > 0;
}

function heroLine(d) {
  const parts = [];
  const server = d.packages.find((p) => p.name === "kalinka-server");
  if (server) parts.push(`Kalinka ${server.version}`);
  if (d.host.dietpi) parts.push(`DietPi ${d.host.dietpi}`);
  else if (d.host.os) parts.push(d.host.os);
  if (d.host.uptime_seconds) parts.push(`up ${duration(d.host.uptime_seconds)}`);
  return parts.join(" · ");
}

function renderDashboard(d) {
  const h = d.host;
  byId("hero-title").textContent = h.hostname || "Kalinka";
  byId("hero-sub").textContent = heroLine(d);
  byId("bar-host").textContent = h.hostname || "";
  byId("fig-memory").textContent = bytes(d.kalinka.memory);
  byId("fig-cpu").textContent = percent(d.kalinka.cpu_percent);
  byId("fig-host-cpu").textContent = percent(h.cpu_percent);

  const rows = byId("services");
  rows.replaceChildren();
  for (const svc of d.services) {
    const row = byId("service-row").content.firstElementChild.cloneNode(true);
    const name = row.querySelector(".service-name");
    name.textContent = unitName(svc.unit);
    name.title = svc.unit;
    const [label, look] = stateLook(svc.state);
    const chip = row.querySelector(".chip");
    chip.textContent = label;
    chip.className = `chip ${look}`;
    row.querySelector(".mem").textContent = bytes(svc.memory);
    row.querySelector(".cpu").textContent = percent(svc.cpu_percent);
    rows.append(row);
  }

  const host = byId("host");
  host.replaceChildren();
  fact(host, "Name", h.hostname);
  fact(host, "System", h.dietpi ? `DietPi ${h.dietpi} · ${h.os}` : h.os);
  fact(host, "Kernel", h.kernel);
  fact(host, "Up for", h.uptime_seconds ? duration(h.uptime_seconds) : "");
  fact(host, "Load", h.load.map((l) => l.toFixed(2)).join("  "));
  fact(host, "Processors", h.cpus ? String(h.cpus) : "");
  fact(host, "Temperature", h.temperature_c == null ? "" : `${h.temperature_c.toFixed(1)} °C`);
  fact(host, "Nearby setup", status ? SETUP[status.setup] : "");
  meter("mem", h.memory_total - h.memory_available, h.memory_total);
  meter("disk", h.disk_total - h.disk_free, h.disk_total);

  versions("packages", d.packages, (name) => name);
  versions("plugins", d.plugins, (name) => name.replace(/^kalinka[-_]plugin[-_]/i, ""));
  byId("python").textContent = d.python ? `Python ${d.python} in the server's environment` : "";
}

async function refreshStatus() {
  try {
    status = await getJSON("/v1/status");
    byId("unreachable").hidden = true;
  } catch (_) {
    byId("unreachable").hidden = busy;
  }
  renderStatus();
}

async function refreshDashboard() {
  try {
    renderDashboard(await getJSON("/v1/dashboard"));
  } catch (_) {
    // The status poll reports an unreachable supervisor.
  }
}

function poll(refresh, ms) {
  const tick = async () => {
    if (!document.hidden) await refresh();
    setTimeout(tick, ms);
  };
  tick();
}

let toastTimer = 0;
function toast(message) {
  const el = byId("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 6000);
}

function confirmAction(action) {
  const a = ACTIONS[action];
  byId("confirm-tile").className = `tile big${a.danger ? " danger" : ""}`;
  byId("confirm-icon").setAttribute("href", `#${a.icon}`);
  byId("confirm-title").textContent = a.title;
  byId("confirm-body").textContent = a.body;
  const go = byId("confirm-go");
  go.textContent = a.go;
  go.onclick = () => {
    closeConfirm();
    start(action);
  };
  byId("confirm").hidden = false;
  byId("confirm-cancel").focus();
}

function closeConfirm() {
  byId("confirm").hidden = true;
}

async function start(action) {
  const before = status;
  try {
    await post(action);
  } catch (e) {
    toast(e.message);
    return;
  }
  ACTIONS[action].follow(before);
}

function showProgress({ title, body, log = false }) {
  busy = true;
  byId("progress-tile").className = "tile big working";
  byId("progress-icon").setAttribute("href", "#i-restart");
  byId("progress-title").textContent = title;
  byId("progress-body").textContent = body;
  const fill = byId("progress-fill");
  fill.className = "bar-fill indefinite";
  fill.style.width = "";
  byId("progress-log").hidden = !log;
  byId("progress").hidden = false;
  renderStatus();
}

function finishProgress({ ok, title, body }) {
  byId("progress-tile").className = `tile big ${ok ? "done" : "danger"}`;
  byId("progress-icon").setAttribute("href", ok ? "#i-check" : "#i-alert");
  byId("progress-title").textContent = title;
  byId("progress-body").textContent = body;
  const fill = byId("progress-fill");
  fill.className = "bar-fill";
  fill.style.width = "100%";
}

function hideProgress() {
  flow++;
  busy = false;
  byId("progress").hidden = true;
  refreshStatus();
}

function showLogText(text, fallback) {
  const pre = byId("progress-log");
  const following = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  pre.textContent = text || fallback;
  if (following) pre.scrollTop = pre.scrollHeight;
}

async function followRestart() {
  const token = ++flow;
  showProgress({ title: "Restarting server", body: "Waiting for Kalinka's server to stop and start again…" });
  const started = Date.now();
  let wentDown = false;
  while (token === flow) {
    await sleep(1500);
    let s;
    try {
      s = await getJSON("/v1/status");
    } catch (_) {
      continue;
    }
    status = s;
    const elapsed = Date.now() - started;
    if (s.core !== "active") wentDown = true;
    if (s.core === "active" && (wentDown || elapsed > 10000)) {
      finishProgress({ ok: true, title: "Server restarted", body: "Kalinka's server is running again." });
      return;
    }
    if (s.core === "failed") {
      finishProgress({ ok: false, title: "The server did not start", body: "It stopped with an error. Restart it again, or reinstall Kalinka." });
      return;
    }
    if (elapsed > 180000) {
      finishProgress({ ok: false, title: "Still starting", body: "The server has not come back after three minutes. It may still be installing packages." });
      return;
    }
  }
}

// An accepted shutdown that systemd then refused clears pending while the supervisor still answers.
async function shutdownAbandoned() {
  try {
    status = await getJSON("/v1/status");
    return status.pending == null;
  } catch (_) {
    return false;
  }
}

async function followReboot() {
  const token = ++flow;
  showProgress({ title: "Restarting the box", body: "The player is shutting down. This page reconnects when it is back." });
  const started = Date.now();
  let wentDown = false;
  while (token === flow) {
    await sleep(2000);
    try {
      await getJSON("/info");
      if (wentDown) {
        finishProgress({ ok: true, title: "The box is back", body: "Reloading this page…" });
        await sleep(1200);
        location.reload();
        return;
      }
    } catch (_) {
      wentDown = true;
    }
    if (!wentDown && (await shutdownAbandoned())) {
      finishProgress({ ok: false, title: "The box did not restart", body: "systemd did not carry out the request. Try again." });
      return;
    }
    if (Date.now() - started > 300000) {
      finishProgress({ ok: false, title: "Not back yet", body: "The box has not answered for five minutes. Check that it has power and a network connection." });
      return;
    }
  }
}

async function followPowerOff() {
  const token = ++flow;
  showProgress({ title: "Powering off", body: "The player is shutting down…" });
  const started = Date.now();
  while (token === flow) {
    await sleep(2000);
    try {
      await getJSON("/info");
    } catch (_) {
      finishProgress({ ok: true, title: "Powered off", body: "Once its lights stop blinking, the box can be unplugged. Plug it back in to start it again." });
      return;
    }
    if (await shutdownAbandoned()) {
      finishProgress({ ok: false, title: "The box did not power off", body: "systemd did not carry out the request. Try again." });
      return;
    }
    if (Date.now() - started > 120000) {
      finishProgress({ ok: false, title: "Still shutting down", body: "The box is still answering after two minutes. Give it a little longer before unplugging it." });
      return;
    }
  }
}

async function followReinstall(before) {
  const token = ++flow;
  const previous = (before || status || { reinstall: {} }).reinstall.finished_at;
  showProgress({
    title: "Reinstalling Kalinka",
    body: "Downloading and installing. This takes several minutes, and this page can stay open.",
    log: true,
  });
  let seenRunning = false;
  while (token === flow) {
    try {
      const [s, log] = await Promise.all([getJSON("/v1/status"), getLog()]);
      if (token !== flow) return;
      status = s;
      showLogText(log, "Waiting for the installer to start…");
      const r = s.reinstall;
      if (r.state === "running") {
        seenRunning = true;
      } else if (r.state !== "idle" && (seenRunning || r.finished_at !== previous)) {
        if (r.state === "succeeded") {
          finishProgress({ ok: true, title: "Kalinka reinstalled", body: "The server is starting again with a fresh installation." });
        } else {
          finishProgress({ ok: false, title: "The reinstall failed", body: "The installer's output is below." });
        }
        return;
      }
    } catch (_) {
      // The supervisor itself may restart while packages install.
    }
    await sleep(2000);
  }
}

async function showFailedLog() {
  ++flow;
  showProgress({ title: "The last reinstall failed", body: "", log: true });
  finishProgress({ ok: false, title: "The last reinstall failed", body: "Its output is below. Reinstall again once the cause is fixed." });
  try {
    showLogText(await getLog(), "No output was recorded.");
  } catch (_) {
    showLogText("", "The log could not be read.");
  }
}

function init() {
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => confirmAction(button.dataset.action));
  }
  byId("confirm-cancel").addEventListener("click", closeConfirm);
  byId("confirm").addEventListener("click", (e) => {
    if (e.target === byId("confirm")) closeConfirm();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeConfirm();
  });
  byId("progress-dismiss").addEventListener("click", hideProgress);
  getJSON("/info")
    .then((i) => {
      info = i;
      byId("foot").textContent = `Kalinka Supervisor ${i.version} · control protocol ${i.protocol}`;
      renderStatus();
    })
    .catch(() => {
      byId("unreachable").hidden = false;
    });
  poll(refreshStatus, 3000);
  poll(refreshDashboard, 5000);
}

init();

// OpenBot server web page. Talks to the same API as the OpenBot app (openbot/server/app.py).
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "openbot.token";
const ROLE = { VIEWER: 1, OPERATOR: 2, ADMIN: 3 };
const STATE_LABELS = {
  queued: "Queued", waiting_for_plate: "Waiting: clear the plate", printing: "Printing",
  done: "Done", failed: "Failed", cancelled: "Cancelled",
};

let token = null;
let info = null;
let me = null;
let ws = null;
let rpcId = 0;
const pending = new Map();
let reconnectDelay = 1000;

// ---------------------------------------------------------------- helpers

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

function loadToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
}
function saveToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {}
}

function withToken(url) {
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
}

// ---------------------------------------------------------------- start-up

async function main() {
  // "#token=..." lets the OpenBot app open this page already signed in.
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    saveToken(decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname);
  }
  token = loadToken();
  info = await (await fetch("/api/info")).json();
  showProject(info.project || {});
  $("server-line").textContent = `Server “${info.server_name}”`;
  if (token && await tokenWorks()) {
    connect();
  } else {
    saveToken(null);
    token = null;
    showPairing();
  }
}

function showProject(p) {
  if (p.publisher_url) $("publisher").href = p.publisher_url;
  if (p.publisher) $("publisher").textContent = p.publisher;
  if (p.repo_url) $("repo").href = p.repo_url;
  if (p.homepage_url) $("homepage").href = p.homepage_url;
  const d = $("donate");
  d.textContent = `♥ Support OpenBot, suggested $${p.suggested_donation_usd || 20}`;
  if (p.donate_url) {
    d.href = p.donate_url;
  } else {
    d.addEventListener("click", (ev) => {
      ev.preventDefault();
      toast("Donations are being set up. Visit " + (p.publisher_url || "Lighthouse Consulting") + " for now.");
    });
  }
}

async function tokenWorks() {
  const r = await fetch("/api/jobs", { headers: { Authorization: "Bearer " + token } });
  return r.ok;
}

// ---------------------------------------------------------------- pairing

function showPairing() {
  $("app").hidden = true;
  $("pair").hidden = false;
  $("pair-name").value ||= "Browser on " + (navigator.platform || "this device");
  if (info.needs_setup) $("pair").querySelector("details").open = true;
}

$("pair-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("pair-error").hidden = true;
  const body = { name: $("pair-name").value.trim() };
  const code = $("setup-code").value.trim();
  if (code) body.setup_code = code;
  const r = await fetch("/api/pair", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) {
    $("pair-error").textContent = await r.text();
    $("pair-error").hidden = false;
    return;
  }
  const req = await r.json();
  $("pair-form").hidden = true;
  if (req.code) {
    $("pair-code").textContent = req.code.slice(0, 3) + " " + req.code.slice(3);
    $("pair-wait").hidden = false;
  }
  pollPairing(req.id);
});

async function pollPairing(id) {
  for (;;) {
    const r = await (await fetch("/api/pair/" + encodeURIComponent(id))).json();
    if (r.status === "approved" && r.token) {
      token = r.token;
      saveToken(token);
      $("pair").hidden = true;
      connect();
      return;
    }
    if (["denied", "expired", "unknown"].includes(r.status)) {
      $("pair-wait").hidden = true;
      $("pair-form").hidden = false;
      $("pair-error").textContent = "Request " + r.status + ". You can ask again.";
      $("pair-error").hidden = false;
      return;
    }
    await new Promise((res) => setTimeout(res, 1500));
  }
}

// ---------------------------------------------------------------- live connection

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/api/ws?token=${encodeURIComponent(token)}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id !== undefined && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      return;
    }
    onEvent(msg.event, msg.data);
  };
  ws.onopen = () => { reconnectDelay = 1000; };
  ws.onclose = async (ev) => {
    for (const { reject } of pending.values()) reject(new Error("disconnected"));
    pending.clear();
    if (ev.code === 4001 || !(await tokenWorks().catch(() => true))) {
      saveToken(null);
      token = null;
      toast("This browser's access was removed.");
      showPairing();
      return;
    }
    $("printer-state").textContent = "reconnecting…";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };
}

function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++rpcId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function act(method, params, done) {
  try {
    await call(method, params);
    if (done) toast(done);
  } catch (e) {
    toast(e.message);
  }
}

function onEvent(event, data) {
  if (event === "hello") {
    me = data.client;
    $("app").hidden = false;
    $("who").textContent = `${me.name} · ${me.role_name}`;
    document.querySelectorAll(".operator-only").forEach((e) => { e.hidden = me.role < ROLE.OPERATOR; });
    document.querySelectorAll(".admin-only").forEach((e) => { e.hidden = me.role < ROLE.ADMIN; });
    showPrinter(data.printer);
    showQueue(data.queue);
    if (data.printer.connected) call("status").then(showStatus).catch(() => {});
    if (me.role >= ROLE.ADMIN) refreshDevices();
  } else if (event === "status") {
    showStatus(data);
  } else if (event === "queue") {
    showQueue(data);
  } else if (event === "printer") {
    showPrinter(data);
  } else if (event === "pairing") {
    showPending(data);
  }
}

// ---------------------------------------------------------------- printer

function showPrinter(p) {
  $("printer-name").textContent = p.name || "Printer";
  const s = $("printer-state");
  s.textContent = p.connected ? "online" : "offline";
  s.className = "pill " + (p.connected ? "ok" : "off");
  const camera = (p.capabilities || []).includes("camera");
  $("camera-card").hidden = !camera;
  const ext = p.machine === "replicator_plus" ? ".makerbot" : ".gcode,.gco,.g";
  $("upload-file").accept = ext;
}

function showStatus(st) {
  const ext = (st.extruders || [])[0];
  if (ext) $("t-nozzle").textContent = `${Math.round(ext.current_temperature)} / ${Math.round(ext.target_temperature)} °C`;
  if (st.bed) {
    $("bed-row").hidden = false;
    $("t-bed").textContent = `${Math.round(st.bed[0])} / ${Math.round(st.bed[1])} °C`;
  }
  const p = st.process;
  const pause = $("btn-pause");
  const cancel = $("btn-cancel");
  if (!p) {
    $("process").textContent = "Idle";
    $("progress-fill").style.width = "0";
    pause.disabled = cancel.disabled = true;
    return;
  }
  const what = p.name === "PrintProcess" ? "Printing" : p.name === "SDPrint" ? "Printing from SD" : p.name;
  $("process").textContent = `${what}: ${(p.step || "").replace(/_/g, " ")}` +
    (p.progress != null ? ` · ${p.progress}%` : "");
  $("progress-fill").style.width = (p.progress || 0) + "%";
  const busy = !p.complete;
  pause.disabled = !busy;
  pause.textContent = p.step === "suspended" ? "Resume" : "Pause";
  cancel.disabled = !(busy && p.cancellable);
}

$("btn-pause").addEventListener("click", () => {
  const resume = $("btn-pause").textContent === "Resume";
  act(resume ? "resume" : "pause", {}, resume ? "Resuming" : "Pausing");
});
$("btn-cancel").addEventListener("click", () => {
  if (confirm("Cancel the current print?")) act("cancel", {}, "Cancelling");
});

// ---------------------------------------------------------------- camera

$("btn-camera").addEventListener("click", () => {
  const img = $("camera");
  if (img.hidden) {
    img.src = withToken("/api/camera.mjpeg");
    img.hidden = false;
    $("btn-camera").textContent = "Stop camera";
  } else {
    img.removeAttribute("src");
    img.hidden = true;
    $("btn-camera").textContent = "Start camera";
  }
});

// ---------------------------------------------------------------- queue

function showQueue(jobs) {
  const body = $("queue");
  body.replaceChildren();
  $("queue-empty").hidden = jobs.length > 0;
  const operator = me && me.role >= ROLE.OPERATOR;
  for (const j of jobs) {
    const tr = el("tr");
    tr.append(el("td", j.name));
    const state = el("td", STATE_LABELS[j.state] || j.state, "state-" + j.state);
    if (j.state === "printing") state.textContent += ` ${j.progress || 0}%`;
    tr.append(state, el("td", j.submitted_by));
    const cell = el("td");
    if (operator && j.state === "waiting_for_plate") {
      const b = el("button", "Plate is clear: start", "primary");
      b.onclick = () => {
        if (confirm(`Start ${j.name}? The printer will heat and move. Is the build plate clear?`)) {
          act("queue.confirm_plate", { job_id: j.id }, "Starting");
        }
      };
      cell.append(b);
    }
    if (operator && ["queued", "waiting_for_plate", "printing"].includes(j.state)) {
      const b = el("button", "Cancel");
      b.onclick = () => confirm(`Cancel ${j.name}?`) && act("queue.cancel", { job_id: j.id });
      cell.append(b);
    }
    if (operator && j.state !== "printing") {
      const b = el("button", "Remove");
      b.onclick = () => act("queue.remove", { job_id: j.id });
      cell.append(b);
    }
    if (j.has_snapshot) {
      const a = el("a", "Photo");
      a.href = withToken(`/api/jobs/${encodeURIComponent(j.id)}/snapshot.jpg`);
      a.target = "_blank";
      cell.append(" ", a);
    }
    tr.append(cell);
    body.append(tr);
  }
}

$("upload-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const file = $("upload-file").files[0];
  if (!file) return;
  const bar = $("upload-progress");
  const xhr = new XMLHttpRequest();          // XHR, for upload progress
  xhr.open("POST", "/api/jobs?name=" + encodeURIComponent(file.name));
  xhr.setRequestHeader("Authorization", "Bearer " + token);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = (100 * e.loaded) / e.total; };
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status === 201) {
      toast("Sent to the queue. It starts after someone confirms the plate is clear.");
      $("upload-form").reset();
    } else {
      toast("Not accepted: " + xhr.responseText);
    }
  };
  xhr.onerror = () => { bar.hidden = true; toast("Upload failed"); };
  bar.value = 0;
  bar.hidden = false;
  xhr.send(file);
});

// ---------------------------------------------------------------- devices (admins)

async function refreshDevices() {
  try {
    showPending(await call("pairing.list"));
    showClients(await call("clients.list"));
  } catch (e) { /* not an admin */ }
}

function roleSelect() {
  const s = el("select");
  for (const [label, value] of [["Viewer", 1], ["Operator", 2], ["Admin", 3]]) {
    const o = el("option", label);
    o.value = value;
    if (value === 2) o.selected = true;
    s.append(o);
  }
  return s;
}

function showPending(list) {
  const ul = $("pending");
  ul.replaceChildren();
  $("pending-empty").hidden = list.length > 0;
  for (const p of list) {
    const li = el("li");
    li.append(el("span", `${p.name}: code ${p.code.slice(0, 3)} ${p.code.slice(3)}`));
    const controls = el("span");
    const role = roleSelect();
    const allow = el("button", "Allow", "primary");
    allow.onclick = async () => {
      await act("pairing.approve", { request_id: p.id, role: Number(role.value) }, "Allowed");
      refreshDevices();
    };
    const deny = el("button", "Deny");
    deny.onclick = async () => { await act("pairing.deny", { request_id: p.id }); refreshDevices(); };
    controls.append(role, " ", allow, " ", deny);
    li.append(controls);
    ul.append(li);
  }
}

function showClients(list) {
  const ul = $("clients");
  ul.replaceChildren();
  for (const c of list.filter((c) => !c.console)) {
    const li = el("li");
    li.append(el("span", `${c.name} · ${c.role_name}`));
    if (!me || c.id !== me.id) {
      const b = el("button", "Remove access", "danger");
      b.onclick = async () => {
        if (confirm(`Remove ${c.name}'s access?`)) {
          await act("clients.revoke", { client_id: c.id }, "Removed");
          refreshDevices();
        }
      };
      li.append(b);
    }
    ul.append(li);
  }
}

main().catch((e) => { $("server-line").textContent = "Can't reach the server: " + e.message; });

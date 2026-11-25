// Vocivio desktop UI (P1b). Uses the global Tauri API (withGlobalTauri = true).
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const transcriptEl = $("transcript");
const connectBtn = $("connect");
const disconnectBtn = $("disconnect");
const killBtn = $("kill");
const inputDeviceEl = $("input-device");
const outputDeviceEl = $("output-device");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status " + (cls || "idle");
}

// Trust-UX state (B3): the pill is composed from the last connection phase reported by the core
// plus the local kill-switch override. The kill switch wins, since it's an explicit operator action.
let lastStatus = { status: "idle", detail: "" };
let killActive = false;

function renderPill() {
  if (killActive) {
    setStatus("KILL SWITCH — passthrough (UNTRANSLATED)", "kill");
    document.body.classList.toggle("killed", true);
    document.body.classList.toggle("degraded", false);
    return;
  }
  document.body.classList.toggle("killed", false);
  const { status, detail } = lastStatus;
  const cls =
    status === "error" ? "error" :
    status === "reconnecting" ? "degraded" :
    status === "ready" ? "ok" :
    status === "idle" ? "idle" : "busy";
  setStatus(detail ? `${status}: ${detail}` : status, cls);
  // Reconnecting == fail-open passthrough: outline the window amber so it's unmissable.
  document.body.classList.toggle("degraded", status === "reconnecting");
}

function setConnected(on) {
  connectBtn.disabled = on;
  disconnectBtn.disabled = !on;
  killBtn.disabled = !on;
  // Pick devices once, before connecting — no hot-swap. To change, Stop then Connect again.
  inputDeviceEl.disabled = on;
  outputDeviceEl.disabled = on;
}

// Settings persistence (Phase E). Non-secret prefs only — server URL, languages, device choices —
// in the webview's app-scoped localStorage. The token is NEVER stored here; it lives in the OS
// keychain (Rust side). An empty token field on launch makes the backend reuse the stored bootstrap.
const SETTINGS_KEY = "vocivio.settings";

function saveSettings() {
  const s = {
    server: $("server").value.trim(),
    source: $("source").value,
    target: $("target").value,
    inputDevice: inputDeviceEl.value,
    outputDevice: outputDeviceEl.value,
  };
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch (e) {
    console.error("saveSettings failed:", e);
  }
}

function loadSettings() {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  } catch {
    return {};
  }
}

// Restore the text/select prefs that don't depend on device enumeration.
(function restoreBasicSettings() {
  const s = loadSettings();
  if (s.server) $("server").value = s.server;
  if (s.source) $("source").value = s.source;
  if (s.target) $("target").value = s.target;
})();

// Populate the device dropdowns from the host's audio devices (Phase C). Enumerated once on open;
// an empty value means "host default". Failure leaves just the Default option — connect still works.
// Device prefs are restored here, after the options exist (only if still present).
async function loadDevices() {
  try {
    const { inputs, outputs } = await invoke("list_devices");
    const fill = (sel, names) => {
      for (const name of names) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        sel.appendChild(opt);
      }
    };
    fill(inputDeviceEl, inputs);
    fill(outputDeviceEl, outputs);
    const s = loadSettings();
    if (s.inputDevice && inputs.includes(s.inputDevice)) inputDeviceEl.value = s.inputDevice;
    if (s.outputDevice && outputs.includes(s.outputDevice)) outputDeviceEl.value = s.outputDevice;
  } catch (e) {
    console.error("list_devices failed:", e);
  }
}
loadDevices();

connectBtn.addEventListener("click", async () => {
  try {
    lastStatus = { status: "connecting", detail: "" };
    killActive = false;
    killBtn.textContent = "Kill switch — pass call through untranslated";
    setConnected(true);
    renderPill();
    saveSettings();
    await invoke("connect", {
      server: $("server").value.trim(),
      token: $("token").value.trim(),
      source: $("source").value,
      target: $("target").value,
      inputDevice: inputDeviceEl.value || null,
      outputDevice: outputDeviceEl.value || null,
    });
  } catch (e) {
    lastStatus = { status: "error", detail: String(e) };
    setConnected(false);
    renderPill();
  }
});

disconnectBtn.addEventListener("click", async () => {
  try {
    await invoke("disconnect");
  } finally {
    setConnected(false);
    killActive = false;
    lastStatus = { status: "idle", detail: "" };
    renderPill();
  }
});

// Kill switch: force the call to pass through untranslated (and stop sending to the server) on a
// live connection, or release it. The connection stays up so translation resumes on release.
async function applyKill(active) {
  killActive = active;
  killBtn.textContent = active
    ? "Resume translation"
    : "Kill switch — pass call through untranslated";
  killBtn.classList.toggle("active", active);
  renderPill();
  try {
    await invoke("set_kill", { active });
  } catch (err) {
    console.error(err);
  }
}

killBtn.addEventListener("click", () => applyKill(!killActive));

const redactEl = $("redact");

async function applyRedaction(active) {
  redactEl.checked = active;
  document.body.classList.toggle("redacting", active);
  try {
    await invoke("set_redaction", { active });
  } catch (err) {
    console.error(err);
  }
}

redactEl.addEventListener("change", (e) => applyRedaction(e.target.checked));

// Hands-free hotkey (Ctrl+Shift+R): toggle redaction without reaching for the mouse —
// the agent can mute audio to the server the instant a card number is about to be spoken.
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.shiftKey && (e.key === "R" || e.key === "r")) {
    e.preventDefault();
    applyRedaction(!redactEl.checked);
  }
});

// Events from the Rust core. The reconnect loop reports phases (connecting/ready/reconnecting);
// the kill-switch override is layered on in renderPill().
listen("status", (e) => {
  const { status, detail } = e.payload;
  if (status === "disconnected") {
    setConnected(false);
    killActive = false;
    lastStatus = { status: "idle", detail: "" };
    renderPill();
    return;
  }
  lastStatus = { status, detail: detail || "" };
  renderPill();
});

listen("transcript", (e) => {
  const { original, translated, latency_ms } = e.payload;
  const row = document.createElement("div");
  row.className = "line";
  row.innerHTML =
    `<span class="orig">${original}</span>` +
    `<span class="arrow">→</span>` +
    `<span class="trans">${translated}</span>` +
    `<span class="lat">${Math.round(latency_ms)}ms</span>`;
  transcriptEl.prepend(row);
});

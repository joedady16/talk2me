const $ = (id) => document.getElementById(id);
const readerAudio = new Audio();
const voiceAudio = new Audio();
const state = { doc: null, index: 0, playing: false, user: null, signup: false };

const setStatus = (msg) => { $("status").textContent = msg; };

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function request(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    showAuth();
    throw new HttpError(401, "Please sign in");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new HttpError(res.status, detail || `${res.status} ${res.statusText}`);
  }
  return res;
}

const api = async (path, options) => {
  const res = await request(path, options);
  return res.status === 204 ? null : res.json();
};

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const timezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
const fmtTime = (iso) => new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 8000);
}

// ---- auth ----------------------------------------------------------------

async function showAuth() {
  $("app").hidden = true;
  $("auth").hidden = false;
  stopReminderPolling();
  const { signup_open } = await api("/api/auth/status");
  setAuthMode(signup_open && state.signup);
  $("auth-switch").hidden = !signup_open;
}

function setAuthMode(signup) {
  state.signup = signup;
  $("auth-title").textContent = signup ? "Create your account" : "Sign in";
  $("auth-submit").textContent = signup ? "Create account" : "Sign in";
  $("auth-name").hidden = !signup;
  $("auth-password").autocomplete = signup ? "new-password" : "current-password";
  $("auth-switch").innerHTML = signup
    ? 'Have an account? <a id="to-login">Sign in</a>'
    : 'New here? <a id="to-signup">Create an account</a>';
  $("auth-error").textContent = "";
  const link = $("to-login") || $("to-signup");
  link.onclick = () => setAuthMode(!signup);
}

$("auth").onsubmit = async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  const body = { email: $("auth-email").value, password: $("auth-password").value, timezone: timezone() };
  if (state.signup) body.display_name = $("auth-name").value;
  try {
    state.user = await postJson(state.signup ? "/api/auth/signup" : "/api/auth/login", body);
    $("auth-password").value = "";
    await startApp();
  } catch (err) {
    $("auth-error").textContent = err.message;
  }
};

$("logout").onclick = async () => {
  pause();
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  state.user = null;
  closeDoc();
  $("chat").innerHTML = "";
  showAuth();
};

// ---- startup -------------------------------------------------------------

async function waitForModels() {
  for (;;) {
    try {
      const health = await api("/api/health");
      if (health.ready) return health;
    } catch { /* server still starting */ }
    setStatus("Loading voice model (first run downloads ~340 MB)…");
    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function loadVoices() {
  const { voices, default: fallback } = await api("/api/voices");
  const saved = localStorage.getItem("voice");
  $("voice").innerHTML = voices.map((v) => `<option>${v}</option>`).join("");
  $("voice").value = voices.includes(saved) ? saved : fallback;
  $("speed").value = localStorage.getItem("speed") || "1.0";
}

async function startApp() {
  $("auth").hidden = true;
  $("app").hidden = false;
  $("who").textContent = state.user.display_name || state.user.email;
  const health = await waitForModels();
  await loadVoices();
  await refreshDocs();
  $("assistant-hint").textContent = health.assistant ? "" : "Assistant disabled: set ANTHROPIC_API_KEY on the server.";
  setStatus("Ready");
  startReminderPolling();
}

// ---- tabs ----------------------------------------------------------------

document.querySelectorAll("nav button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b === btn));
    $("reader-view").hidden = btn.dataset.view !== "reader";
    $("assistant-view").hidden = btn.dataset.view !== "assistant";
    if (btn.dataset.view === "assistant") openAssistant();
  };
});

// ---- reader: document list -----------------------------------------------

async function refreshDocs() {
  const docs = await api("/api/documents");
  $("docs").innerHTML = "";
  for (const d of docs) {
    const li = document.createElement("li");
    li.className = state.doc?.id === d.id ? "active" : "";
    li.innerHTML = `<span class="title"></span><span class="progress"></span><button class="delete" title="Delete">×</button>`;
    li.querySelector(".title").textContent = d.title;
    li.querySelector(".progress").textContent = `${Math.round((d.position / Math.max(d.total, 1)) * 100)}%`;
    li.onclick = () => openDoc(d.id);
    li.querySelector(".delete").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${d.title}"?`)) return;
      await api(`/api/documents/${d.id}`, { method: "DELETE" });
      if (state.doc?.id === d.id) closeDoc();
      refreshDocs();
    };
    $("docs").appendChild(li);
  }
}

async function upload(file) {
  setStatus(`Reading ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const doc = await api("/api/documents", { method: "POST", body: form });
    setStatus(`Added "${doc.title}" (${doc.total} sentences)`);
    await refreshDocs();
    openDoc(doc.id);
  } catch (err) {
    setStatus(`Upload failed: ${err.message}`);
  }
}

// ---- reader: playback ----------------------------------------------------

const audioUrl = (i) => {
  const params = new URLSearchParams({ voice: $("voice").value, speed: $("speed").value });
  return `/api/documents/${state.doc.id}/audio/${i}?${params}`;
};

async function openDoc(id) {
  pause();
  state.doc = await api(`/api/documents/${id}`);
  state.index = Math.min(state.doc.position, state.doc.sentences.length - 1);
  const text = $("text");
  text.innerHTML = "";
  state.doc.sentences.forEach((s, i) => {
    const span = document.createElement("span");
    span.className = "s";
    span.textContent = s + " ";
    span.onclick = () => playFrom(i);
    text.appendChild(span);
  });
  for (const b of ["play", "prev", "next"]) $(b).disabled = false;
  highlight();
  refreshDocs();
  setStatus(`${state.doc.title} · ${state.doc.sentences.length} sentences`);
}

function closeDoc() {
  pause();
  state.doc = null;
  $("text").innerHTML = '<p class="empty">Upload or select a document to start listening.</p>';
  for (const b of ["play", "prev", "next"]) $(b).disabled = true;
}

function highlight() {
  const spans = $("text").querySelectorAll(".s");
  spans.forEach((el, i) => el.classList.toggle("current", i === state.index));
  spans[state.index]?.scrollIntoView({ block: "center", behavior: "smooth" });
}

let saveTimer;
function savePosition() {
  clearTimeout(saveTimer);
  const { id } = state.doc;
  const position = state.index;
  saveTimer = setTimeout(() => {
    api(`/api/documents/${id}/position`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
    }).then(refreshDocs).catch(() => {});
  }, 1000);
}

function prefetch(i) {
  if (i < state.doc.sentences.length) fetch(audioUrl(i)).catch(() => {});
}

async function playFrom(i) {
  if (!state.doc) return;
  state.index = Math.max(0, Math.min(i, state.doc.sentences.length - 1));
  state.playing = true;
  $("play").textContent = "⏸ Pause";
  highlight();
  savePosition();
  readerAudio.src = audioUrl(state.index);
  try {
    await readerAudio.play();
    prefetch(state.index + 1);
  } catch (err) {
    if (err.name !== "AbortError") {
      setStatus(`Playback error: ${err.message}`);
      pause();
    }
  }
}

function pause() {
  state.playing = false;
  readerAudio.pause();
  $("play").textContent = "▶ Play";
}

readerAudio.onended = () => {
  if (state.index + 1 < state.doc.sentences.length) {
    playFrom(state.index + 1);
  } else {
    pause();
    setStatus("Finished");
  }
};

$("play").onclick = () => (state.playing ? pause() : playFrom(state.index));
$("prev").onclick = () => playFrom(state.index - 1);
$("next").onclick = () => playFrom(state.index + 1);
$("voice").onchange = (e) => localStorage.setItem("voice", e.target.value);
$("speed").onchange = (e) => localStorage.setItem("speed", e.target.value);

document.addEventListener("keydown", (e) => {
  if (["SELECT", "INPUT", "TEXTAREA"].includes(e.target.tagName) || !state.doc || $("reader-view").hidden) return;
  if (e.code === "Space") { e.preventDefault(); $("play").click(); }
  if (e.code === "ArrowRight") playFrom(state.index + 1);
  if (e.code === "ArrowLeft") playFrom(state.index - 1);
});

const drop = $("drop");
drop.onclick = () => $("file").click();
$("file").onchange = (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; };
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
};

// ---- assistant -----------------------------------------------------------

function addBubble(text, cls) {
  const el = document.createElement("div");
  el.className = `bubble ${cls}`;
  el.textContent = text;
  $("chat").appendChild(el);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return el;
}

async function speak(text) {
  if (!text) return;
  pause();  // don't talk over the reader
  try {
    const res = await request("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: $("voice").value, speed: Number($("speed").value) }),
    });
    URL.revokeObjectURL(voiceAudio.src);
    voiceAudio.src = URL.createObjectURL(await res.blob());
    await voiceAudio.play();
  } catch (err) {
    if (err.name !== "AbortError") console.warn("speak failed", err);
  }
}

function renderList(id, items, render) {
  const ul = $(id);
  ul.innerHTML = items.length ? "" : '<li class="empty">Nothing yet</li>';
  for (const item of items) {
    const li = document.createElement("li");
    const [main, sub] = render(item);
    li.textContent = main;
    if (sub) {
      const small = document.createElement("small");
      small.textContent = sub;
      li.appendChild(small);
    }
    ul.appendChild(li);
  }
}

async function refreshSidebar() {
  const [reminders, tasks, notes] = await Promise.all([
    api("/api/reminders"), api("/api/tasks"), api("/api/notes"),
  ]);
  renderList("reminders", reminders, (r) => [r.message, fmtTime(r.remind_at)]);
  renderList("tasks", tasks, (t) => [t.title, t.due_at ? `Due ${fmtTime(t.due_at)}` : t.notes]);
  renderList("notes", notes.slice(0, 10), (n) => [n.content, fmtTime(n.created_at)]);
}

async function openAssistant() {
  if (!$("chat").childElementCount) {
    const transcript = await api("/api/assistant/transcript");
    for (const m of transcript) addBubble(m.content, m.role);
    if (!transcript.length) addBubble("Hi! Tap the mic and tell me what you're up to, or what you need to remember.", "assistant");
  }
  refreshSidebar();
}

async function handleTurn(sendRequest, userText) {
  const mine = userText ? addBubble(userText, "user") : addBubble("Transcribing…", "user pending");
  const thinking = addBubble("Thinking…", "assistant pending");
  setComposerBusy(true);
  try {
    const { transcript, reply } = await sendRequest();
    if (!userText) {
      if (transcript) { mine.textContent = transcript; mine.className = "bubble user"; } else mine.remove();
    }
    thinking.textContent = reply;
    thinking.className = "bubble assistant";
    if ($("speak-replies").checked) speak(reply);
    refreshSidebar();
  } catch (err) {
    if (!userText) mine.remove();
    thinking.textContent = `⚠ ${err.message}`;
    thinking.className = "bubble assistant";
  } finally {
    setComposerBusy(false);
  }
}

function setComposerBusy(busy) {
  $("send").disabled = busy;
  $("mic").disabled = busy;
}

$("composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  handleTurn(() => postJson("/api/assistant/message", { text }), text);
};

$("new-chat").onclick = async () => {
  await api("/api/assistant/new", { method: "POST" });
  $("chat").innerHTML = "";
  addBubble("Started a new conversation.", "assistant");
};

// Tap to start recording, tap again to stop and send.
let recorder = null;
$("mic").onclick = async () => {
  if (recorder?.state === "recording") {
    recorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("Microphone needs HTTPS or localhost.");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("Microphone permission was denied.");
    return;
  }
  voiceAudio.pause();
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    $("mic").classList.remove("recording");
    const blob = new Blob(chunks, { type: recorder.mimeType });
    const form = new FormData();
    form.append("audio", blob, "speech");
    handleTurn(() => api("/api/assistant/voice", { method: "POST", body: form }));
  };
  recorder.start();
  $("mic").classList.add("recording");
};

// ---- reminders -----------------------------------------------------------

let reminderTimer = null;

async function checkReminders() {
  try {
    const due = await api("/api/reminders/due", { method: "POST" });
    for (const r of due) {
      const text = `Reminder: ${r.message}`;
      toast(text);
      addBubble(text, "assistant reminder");
      if ("Notification" in window && Notification.permission === "granted") new Notification("Talk2Me", { body: r.message });
      await speak(text);
    }
    if (due.length && !$("assistant-view").hidden) refreshSidebar();
  } catch { /* offline or signed out; try again next tick */ }
}

function startReminderPolling() {
  stopReminderPolling();
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  checkReminders();
  reminderTimer = setInterval(checkReminders, 20000);
}

function stopReminderPolling() {
  clearInterval(reminderTimer);
  reminderTimer = null;
}

// ---- boot ----------------------------------------------------------------

(async () => {
  try {
    state.user = await api("/api/auth/me");
    await startApp();
  } catch {
    showAuth();
  }
})();

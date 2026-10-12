const $ = (id) => document.getElementById(id);
const audio = new Audio();
const state = { doc: null, index: 0, playing: false };

const setStatus = (msg) => { $("status").textContent = msg; };

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.status === 204 ? null : res.json();
}

const audioUrl = (i) => {
  const params = new URLSearchParams({ voice: $("voice").value, speed: $("speed").value });
  return `/api/documents/${state.doc.id}/audio/${i}?${params}`;
};

// ---- startup -------------------------------------------------------------

async function waitForEngine() {
  for (;;) {
    try {
      const { ready } = await api("/api/health");
      if (ready) return;
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

// ---- document list -------------------------------------------------------

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

// ---- reader --------------------------------------------------------------

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
  audio.src = audioUrl(state.index);
  try {
    await audio.play();
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
  audio.pause();
  $("play").textContent = "▶ Play";
}

audio.onended = () => {
  if (state.index + 1 < state.doc.sentences.length) {
    playFrom(state.index + 1);
  } else {
    pause();
    setStatus("Finished");
  }
};

// ---- wiring --------------------------------------------------------------

$("play").onclick = () => (state.playing ? pause() : playFrom(state.index));
$("prev").onclick = () => playFrom(state.index - 1);
$("next").onclick = () => playFrom(state.index + 1);
$("voice").onchange = (e) => localStorage.setItem("voice", e.target.value);
$("speed").onchange = (e) => localStorage.setItem("speed", e.target.value);

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT" || !state.doc) return;
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

(async () => {
  await waitForEngine();
  await loadVoices();
  await refreshDocs();
  setStatus("Ready");
})();

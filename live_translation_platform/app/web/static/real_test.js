const state = {
  lessonId: null,
  latestDiagnostics: null,
  diagnosticsSocket: null,
};

const nodes = {
  form: document.querySelector("#realTestForm"),
  title: document.querySelector("#realTitle"),
  audioSource: document.querySelector("#realAudioSource"),
  sttProvider: document.querySelector("#realSttProvider"),
  translator: document.querySelector("#realTranslator"),
  languages: document.querySelector("#realLanguages"),
  lessonId: document.querySelector("#realLessonId"),
  meetingId: document.querySelector("#realMeetingId"),
  meetingUuid: document.querySelector("#realMeetingUuid"),
  password: document.querySelector("#realPassword"),
  rtmsStatus: document.querySelector("#realRtmsStatus"),
  browserAudioStatus: document.querySelector("#realBrowserAudioStatus"),
  openHostZoom: document.querySelector("#openHostZoom"),
  armRtms: document.querySelector("#armRtms"),
  startPipeline: document.querySelector("#startPipeline"),
  openStudentPage: document.querySelector("#openStudentPage"),
  openTeacherPage: document.querySelector("#openTeacherPage"),
  diagnosticsLog: document.querySelector("#realDiagnosticsLog"),
  captions: document.querySelector("#realCaptions"),
  errors: document.querySelector("#realErrors"),
  copyDiagnostics: document.querySelector("#copyDiagnostics"),
  toast: document.querySelector("#toast"),
};

function toast(message) {
  if (!nodes.toast) return;
  nodes.toast.textContent = message;
  nodes.toast.hidden = false;
  setTimeout(() => {
    nodes.toast.hidden = true;
  }, 4500);
}

function appendLog(message, payload) {
  if (!nodes.diagnosticsLog) return;
  const line = document.createElement("p");
  const suffix = payload ? ` ${JSON.stringify(payload)}` : "";
  line.textContent = `[${new Date().toLocaleTimeString()}] ${message}${suffix}`;
  nodes.diagnosticsLog.prepend(line);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function selectedLanguages() {
  return nodes.languages.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function renderLesson(lesson) {
  state.lessonId = lesson.lesson_id;
  nodes.lessonId.textContent = lesson.lesson_id;
  nodes.meetingId.textContent = lesson.zoom?.meeting_id || "none";
  nodes.meetingUuid.textContent = lesson.zoom?.meeting_uuid || "none";
  nodes.password.textContent = lesson.zoom?.password || "none";
  if (nodes.rtmsStatus) nodes.rtmsStatus.textContent = lesson.rtms_status || "waiting_for_meeting";
  if (nodes.browserAudioStatus) nodes.browserAudioStatus.textContent = lesson.browser_audio_status || "not_connected";
  nodes.openHostZoom.href = lesson.zoom?.start_url || "#";
  nodes.openHostZoom.hidden = !lesson.zoom?.start_url;
  nodes.openStudentPage.href = `/student/${lesson.lesson_id}`;
  nodes.openStudentPage.hidden = false;
  if (nodes.openTeacherPage) {
    nodes.openTeacherPage.href = `/teacher/${lesson.lesson_id}`;
    nodes.openTeacherPage.hidden = false;
  }
  if (nodes.armRtms) nodes.armRtms.disabled = false;
  nodes.startPipeline.disabled = false;
  connectDiagnostics(lesson.lesson_id);
  loadDiagnostics(lesson.lesson_id);
}

async function createRealLesson(event) {
  event.preventDefault();
  const payload = {
    title: nodes.title.value || "Real Zoom RTMS test",
    audio_source: nodes.audioSource.value,
    stt_provider: nodes.sttProvider.value,
    translation_provider: nodes.translator.value,
    target_languages: selectedLanguages(),
  };
  try {
    const created = await readJson(
      await fetch("/api/real-test/create-lesson", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );
    appendLog(`Created real test ${created.real_test_id}`);
    renderLesson(created.lesson);
  } catch (error) {
    toast(error.message);
    appendLog("Create lesson failed", { error: error.message });
  }
}

async function postLessonAction(action) {
  if (!state.lessonId) return;
  try {
    const payload = await readJson(await fetch(`/api/lessons/${state.lessonId}/${action}`, { method: "POST" }));
    appendLog(`${action} accepted`, payload);
    if (payload.rtms_status && nodes.rtmsStatus) nodes.rtmsStatus.textContent = payload.rtms_status;
    await loadDiagnostics(state.lessonId);
  } catch (error) {
    toast(error.message);
    appendLog(`${action} failed`, { error: error.message });
  }
}

async function loadDiagnostics(lessonId) {
  try {
    const payload = await readJson(await fetch(`/api/lessons/${lessonId}/diagnostics`));
    state.latestDiagnostics = payload;
    renderDiagnostics(payload);
  } catch (error) {
    appendLog("Diagnostics load failed", { error: error.message });
  }
}

function renderDiagnostics(payload) {
  if (payload.rtms?.rtms_status && nodes.rtmsStatus) nodes.rtmsStatus.textContent = payload.rtms.rtms_status;
  if (payload.browser_audio?.status && nodes.browserAudioStatus) nodes.browserAudioStatus.textContent = payload.browser_audio.status;
  appendLog("Diagnostics updated", {
    rtms: payload.rtms?.rtms_status,
    browser_audio: payload.browser_audio?.status,
    pipeline: payload.pipeline?.status,
    captions: payload.captions?.sent,
  });
  renderCaptions(payload.latest_captions || []);
  renderErrors(payload.latest_errors || []);
}

function renderCaptions(captions) {
  nodes.captions.innerHTML = "";
  if (!captions.length) {
    nodes.captions.innerHTML = '<p class="muted">No captions yet.</p>';
    return;
  }
  captions.forEach((caption) => {
    const item = document.createElement("div");
    item.className = "caption-item";
    item.innerHTML = `<strong>${caption.created_at}</strong><p>${escapeHtml(caption.original_text || "")}</p><pre>${escapeHtml(JSON.stringify(caption.translations || {}, null, 2))}</pre>`;
    nodes.captions.appendChild(item);
  });
}

function renderErrors(errors) {
  nodes.errors.innerHTML = "";
  if (!errors.length) {
    nodes.errors.innerHTML = '<p class="muted">No errors yet.</p>';
    return;
  }
  errors.forEach((error) => {
    const line = document.createElement("p");
    line.textContent = `[${error.level}] ${error.message}`;
    nodes.errors.appendChild(line);
  });
}

function connectDiagnostics(lessonId) {
  if (state.diagnosticsSocket) state.diagnosticsSocket.close();
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/lessons/${lessonId}/diagnostics`);
  state.diagnosticsSocket = socket;
  socket.onopen = () => appendLog("Diagnostics websocket connected");
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    appendLog(payload.event || "diagnostic_event", payload);
    if (payload.event === "rtms_status" && payload.status) {
      if (nodes.rtmsStatus) nodes.rtmsStatus.textContent = payload.status;
    }
    if (payload.event === "caption_sent") {
      loadDiagnostics(lessonId);
    }
  };
  socket.onerror = () => appendLog("Diagnostics websocket error");
  socket.onclose = () => appendLog("Diagnostics websocket closed");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

nodes.form?.addEventListener("submit", createRealLesson);
nodes.armRtms?.addEventListener("click", () => postLessonAction("arm-rtms"));
nodes.startPipeline?.addEventListener("click", () => postLessonAction("start"));
nodes.copyDiagnostics?.addEventListener("click", async () => {
  await navigator.clipboard.writeText(JSON.stringify(state.latestDiagnostics || {}, null, 2));
  toast("Diagnostics JSON copied");
});

const toast = document.querySelector("#toast");

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  setTimeout(() => {
    toast.hidden = true;
  }, 4500);
}

async function createLesson(mode) {
  const response = await fetch("/api/lessons", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: mode === "mock" ? "C# mock lesson" : "C# Zoom lesson",
      mode,
      stt_provider: "mock",
      translation_provider: "mock",
      target_languages: ["kk", "uz", "zh-Hans"],
    }),
  });
  if (!response.ok) {
    const error = await response.json();
    showToast(error.detail || "Could not create lesson");
    return;
  }
  const lesson = await response.json();
  window.location.href = `/teacher/${lesson.lesson_id}`;
}

document.querySelector("#createMock")?.addEventListener("click", () => createLesson("mock"));
document.querySelector("#createZoom")?.addEventListener("click", () => createLesson("zoom"));

async function postAction(lessonId, action) {
  const response = await fetch(`/api/lessons/${lessonId}/${action}`, { method: "POST" });
  const payload = await response.json();
  if (!response.ok) {
    alert(payload.detail || `Could not ${action} lesson`);
    return;
  }
  const rtmsStatus = document.querySelector("#rtmsStatus");
  if (rtmsStatus) rtmsStatus.textContent = payload.rtms_status;
}

document.querySelector("#startLesson")?.addEventListener("click", (event) => {
  postAction(event.currentTarget.dataset.lessonId, "start");
});

document.querySelector("#stopLesson")?.addEventListener("click", (event) => {
  postAction(event.currentTarget.dataset.lessonId, "stop");
});

const debugLog = document.querySelector("#debugLog");
if (debugLog) {
  const lessonId = debugLog.dataset.lessonId;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/lessons/${lessonId}/debug`);
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    const line = document.createElement("p");
    line.textContent = `[${payload.level}] ${payload.message}`;
    debugLog.prepend(line);
    if (payload.event === "rtms_status" && payload.status) {
      renderRtmsStatus({ rtms_status: payload.status, rtms_error: payload.message });
    }
  };
}

async function loadRtmsStatus(lessonId) {
  const response = await fetch(`/api/lessons/${lessonId}/rtms`);
  if (!response.ok) return;
  renderRtmsStatus(await response.json());
}

async function startRtms(lessonId) {
  const response = await fetch(`/api/lessons/${lessonId}/start-rtms`, { method: "POST" });
  const payload = await response.json();
  if (!response.ok) {
    renderRtmsStatus({ rtms_error: payload.detail, rtms_status: "not_configured" });
    return;
  }
  renderRtmsStatus(payload);
}

function renderRtmsStatus(payload) {
  const fields = {
    "#rtmsPanelStatus": payload.rtms_status,
    "#rtmsStreamId": payload.rtms_stream_id,
    "#rtmsAudioChunks": payload.audio_chunks_received,
    "#rtmsAudioDropped": payload.audio_chunks_dropped,
    "#rtmsAudioQueueSize": payload.audio_queue_size,
    "#rtmsTranscriptEvents": payload.transcript_events_received,
    "#pipelineStatus": payload.pipeline_status,
    "#pipelineAudioSource": payload.pipeline_audio_source,
    "#pipelineChunksProcessed": payload.pipeline_chunks_processed,
    "#sttEventsGenerated": payload.stt_events_generated,
    "#captionsSent": payload.captions_sent,
    "#sttProviderStatus": payload.stt_provider_status,
    "#sttProviderChunksSent": payload.stt_provider_audio_chunks_sent,
    "#sttProviderBytesSent": payload.stt_provider_audio_bytes_sent,
    "#sttProviderPartials": payload.stt_provider_partial_events,
    "#sttProviderFinals": payload.stt_provider_final_events,
    "#sttProviderNoMatch": payload.stt_provider_no_match_count,
    "#sttProviderCanceled": payload.stt_provider_canceled_count,
    "#sttProviderLastEvent": payload.stt_provider_last_event_at,
    "#sttProviderLastError": payload.stt_provider_last_error,
    "#sttProviderLastTranscript": payload.stt_provider_last_transcript,
    "#translationRequests": payload.translation_requests_count,
    "#translationErrors": payload.translation_errors_count,
    "#translationAvgLatency": payload.translation_avg_latency_ms !== undefined ? `${payload.translation_avg_latency_ms} ms` : undefined,
    "#translationLastError": payload.translation_last_error,
    "#rtmsLastAudio": payload.rtms_last_audio_at,
    "#rtmsLastTranscript": payload.rtms_last_transcript_at,
    "#rtmsLastError": payload.rtms_error,
  };
  for (const [selector, value] of Object.entries(fields)) {
    const node = document.querySelector(selector);
    if (node && value !== undefined && value !== null) node.textContent = value;
  }
}

document.querySelector("#startRtms")?.addEventListener("click", (event) => {
  startRtms(event.currentTarget.dataset.lessonId);
});

document.querySelector("#refreshRtms")?.addEventListener("click", (event) => {
  loadRtmsStatus(event.currentTarget.dataset.lessonId);
});

const rtmsPanel = document.querySelector(".rtms-panel");
if (rtmsPanel) {
  loadRtmsStatus(rtmsPanel.dataset.lessonId);
}

document.querySelector("#saveTeacherGlossary")?.addEventListener("click", async () => {
  const select = document.querySelector("#teacherGlossary");
  const enabled = document.querySelector("#teacherGlossaryEnabled");
  const lessonId = select?.dataset.lessonId;
  if (!lessonId) return;
  const response = await fetch(`/api/lessons/${lessonId}/glossary`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      glossary_id: select.value || null,
      enabled: Boolean(enabled?.checked),
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    alert(payload.detail || "Could not save glossary");
    return;
  }
  const status = document.querySelector("#teacherGlossaryStatus");
  if (status) status.textContent = `${payload.glossary_id || "none"} / ${payload.enabled ? "enabled" : "disabled"}`;
});

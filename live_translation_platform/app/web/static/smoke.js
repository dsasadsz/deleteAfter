const form = document.querySelector("#smokeForm");
const audioMode = document.querySelector("#audioMode");
const uploadField = document.querySelector("#uploadField");
const streamingModeField = document.querySelector("#streamingModeField");
const audioFile = document.querySelector("#audioFile");
const uploadStatus = document.querySelector("#uploadStatus");
const smokeLog = document.querySelector("#smokeLog");
const toast = document.querySelector("#toast");

let audioSampleId = null;
let smokeSocket = null;

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  setTimeout(() => {
    toast.hidden = true;
  }, 4500);
}

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = value ?? "";
}

function logEvent(payload) {
  if (!smokeLog) return;
  const line = document.createElement("p");
  line.textContent = `[${payload.event}] ${payload.text || payload.error || payload.status || ""}`;
  smokeLog.prepend(line);
}

function updateLatency(latency) {
  if (!latency) return;
  setText("#latFirstPartial", `${latency.first_partial ?? 0} ms`);
  setText("#latSttFinal", `${latency.stt_final ?? 0} ms`);
  setText("#latTranslation", `${latency.translation ?? 0} ms`);
  setText("#latTotalServer", `${latency.total_server ?? 0} ms`);
  setText("#latClientReceive", `${latency.client_receive ?? 0} ms`);
}

function updateAudioMetrics(metrics) {
  if (!metrics) return;
  setText("#metricStreamingMode", metrics.streaming_mode || "none");
  setText("#metricAudioDuration", `${metrics.audio_duration_ms ?? 0} ms`);
  setText("#metricChunks", metrics.chunks_count ?? 0);
  setText("#metricChunkMs", `${metrics.chunk_ms ?? 0} ms`);
  setText("#metricElapsedSend", `${metrics.elapsed_audio_send_ms ?? 0} ms`);
  setText("#metricRealtimeFactor", metrics.realtime_factor ?? 0);
}

function connectSmokeSocket(smokeTestId) {
  if (smokeSocket) smokeSocket.close();
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  smokeSocket = new WebSocket(`${protocol}://${window.location.host}/ws/smoke/${smokeTestId}`);
  smokeSocket.onmessage = async (event) => {
    const payload = JSON.parse(event.data);
    const receivedAt = Date.now();
    logEvent(payload);
    if (payload.lesson_id) setText("#resultLessonId", payload.lesson_id);
    if (payload.event === "stt_partial") setText("#originalText", payload.text);
    if (payload.event === "stt_final") {
      setText("#originalText", payload.text);
      setText("#normalizedText", payload.normalized_text || payload.text);
    }
    if (payload.event === "translation_done") renderTranslations(payload.translations || {});
    if (payload.event === "caption_sent") {
      const latency = { ...(payload.latency_ms || {}) };
      if (payload.websocket_sent_at) {
        latency.client_receive = Math.max(0, receivedAt - Date.parse(payload.websocket_sent_at));
      }
      updateLatency(latency);
    }
    if (payload.event === "smoke_completed" || payload.event === "smoke_error") {
      await refreshResult(smokeTestId);
    }
  };
}

function renderTranslations(translations) {
  setText("#translationKk", translations.kk || "");
  setText("#translationUz", translations.uz || "");
  setText("#translationZh", translations["zh-Hans"] || "");
}

async function uploadAudioIfNeeded() {
  if (audioMode.value !== "wav_upload") return null;
  if (!audioFile.files.length) {
    throw new Error("Choose a WAV file first.");
  }
  const formData = new FormData();
  formData.append("file", audioFile.files[0]);
  const response = await fetch("/api/smoke/upload-audio", { method: "POST", body: formData });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Audio upload failed.");
  audioSampleId = payload.audio_sample_id;
  uploadStatus.textContent = payload.warning || `Uploaded ${payload.chunks} chunks at ${payload.sample_rate} Hz.`;
  return audioSampleId;
}

async function refreshResult(smokeTestId) {
  const response = await fetch(`/api/smoke/${smokeTestId}`);
  if (!response.ok) return;
  const payload = await response.json();
  setText("#smokeStatus", payload.status);
  setText("#resultLessonId", payload.lesson_id || "temporary");
  setText("#originalText", payload.results.original_text);
  setText("#normalizedText", payload.results.original_text_normalized || payload.results.original_text);
  renderTranslations(payload.results.translations || {});
  updateLatency(payload.latency_ms);
  updateAudioMetrics(payload.audio_metrics || payload.provider_metrics?.audio_streaming || {});
  refreshUsage(smokeTestId);
  if (payload.errors?.length) showToast(payload.errors.join(", "));
}

async function refreshUsage(smokeTestId) {
  const response = await fetch(`/api/smoke/${smokeTestId}/usage`);
  if (!response.ok) return;
  const payload = await response.json();
  setText("#smokeUsage", `${payload.audio_minutes} min / ${payload.translation_characters} chars / ${payload.total_estimated_cost} ${payload.currency}`);
}

audioMode?.addEventListener("change", () => {
  uploadField.hidden = audioMode.value !== "wav_upload";
  if (streamingModeField) streamingModeField.hidden = audioMode.value !== "wav_upload";
});

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setText("#smokeStatus", "starting");
    const sampleId = await uploadAudioIfNeeded();
    const lessonId = document.querySelector("#smokeLesson").value || null;
    const targetLanguages = document.querySelector("#targetLanguages").value.split(",").map((item) => item.trim()).filter(Boolean);
    const response = await fetch("/api/smoke/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lesson_id: lessonId,
        audio_mode: audioMode.value,
        streaming_mode: document.querySelector("#streamingMode")?.value || "realtime_stream",
        stt_provider: document.querySelector("#sttProvider").value,
        translation_provider: document.querySelector("#translationProvider").value,
        target_languages: targetLanguages,
        audio_sample_id: sampleId,
        glossary_id: document.querySelector("#smokeGlossary")?.value || null,
        glossary_enabled: Boolean(document.querySelector("#smokeGlossaryEnabled")?.checked && document.querySelector("#smokeGlossary")?.value),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Smoke test failed to start.");
    setText("#smokeId", payload.smoke_test_id);
    setText("#smokeStatus", payload.status);
    connectSmokeSocket(payload.smoke_test_id);
  } catch (error) {
    setText("#smokeStatus", "error");
    showToast(error.message);
  }
});

const params = new URLSearchParams(window.location.search);
const requestedLesson = params.get("lesson_id");
if (requestedLesson) {
  const lessonSelect = document.querySelector("#smokeLesson");
  if (lessonSelect) lessonSelect.value = requestedLesson;
}

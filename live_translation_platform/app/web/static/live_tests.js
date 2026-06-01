const liveState = { runId: null };

const liveNodes = {
  form: document.querySelector("#liveTestForm"),
  lessonId: document.querySelector("#liveLessonId"),
  sttProvider: document.querySelector("#liveSttProvider"),
  translator: document.querySelector("#liveTranslator"),
  chunkMs: document.querySelector("#liveChunkMs"),
  silenceMs: document.querySelector("#liveSilenceMs"),
  maxSegmentMs: document.querySelector("#liveMaxSegmentMs"),
  partials: document.querySelector("#livePartials"),
  phraseLabel: document.querySelector("#livePhraseLabel"),
  expectedText: document.querySelector("#liveExpectedText"),
  actions: document.querySelector("#liveRunActions"),
  activeRun: document.querySelector("#liveActiveRun"),
  teacherUrl: document.querySelector("#liveTeacherUrl"),
  studentUrl: document.querySelector("#liveStudentUrl"),
  refresh: document.querySelector("#liveRefresh"),
  finish: document.querySelector("#liveFinish"),
  captionPreview: document.querySelector("#liveCaptionPreview"),
  notesForm: document.querySelector("#liveNotesForm"),
  transcriptQuality: document.querySelector("#liveTranscriptQuality"),
  translationQuality: document.querySelector("#liveTranslationQuality"),
  qualityNotes: document.querySelector("#liveQualityNotes"),
  toast: document.querySelector("#toast"),
};

function showToast(message) {
  if (!liveNodes.toast) return;
  liveNodes.toast.textContent = message;
  liveNodes.toast.hidden = false;
  setTimeout(() => { liveNodes.toast.hidden = true; }, 3500);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

async function startRun(event) {
  event.preventDefault();
  const payload = {
    lesson_id: liveNodes.lessonId.value,
    stt_provider: liveNodes.sttProvider.value,
    translation_provider: liveNodes.translator.value,
    chunk_ms: Number(liveNodes.chunkMs.value),
    silence_commit_ms: Number(liveNodes.silenceMs.value),
    max_segment_duration_ms: Number(liveNodes.maxSegmentMs.value),
    partials_enabled: liveNodes.partials.value === "true",
    test_phrase_label: liveNodes.phraseLabel.value,
    expected_text: liveNodes.expectedText.value,
  };
  try {
    const created = await readJson(await fetch("/api/live-tests", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
    liveState.runId = created.live_test_id;
    liveNodes.actions.hidden = false;
    liveNodes.activeRun.textContent = `${created.live_test_id} (${created.status})`;
    liveNodes.teacherUrl.href = created.teacher_url;
    liveNodes.studentUrl.href = created.student_url;
    await refreshRun();
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshRun() {
  if (!liveState.runId) return;
  try {
    const run = await readJson(await fetch(`/api/live-tests/${liveState.runId}`));
    liveNodes.activeRun.textContent = `${run.live_test_id} (${run.status}) total=${run.total_latency_ms || "n/a"}ms`;
    liveNodes.captionPreview.textContent = JSON.stringify({ transcript: run.transcript, translations: run.translations, latency_ms: run.total_latency_ms, commit_reason: run.commit_reason }, null, 2);
  } catch (error) {
    showToast(error.message);
  }
}

async function finishRun() {
  if (!liveState.runId) return;
  try {
    await readJson(await fetch(`/api/live-tests/${liveState.runId}/finish`, { method: "POST" }));
    await refreshRun();
  } catch (error) {
    showToast(error.message);
  }
}

async function saveNotes(event) {
  event.preventDefault();
  if (!liveState.runId) return;
  try {
    await readJson(await fetch(`/api/live-tests/${liveState.runId}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript_quality: liveNodes.transcriptQuality.value || null, translation_quality: liveNodes.translationQuality.value || null, quality_notes: liveNodes.qualityNotes.value || null }),
    }));
    showToast("Notes saved");
    await refreshRun();
  } catch (error) {
    showToast(error.message);
  }
}

liveNodes.form?.addEventListener("submit", startRun);
liveNodes.refresh?.addEventListener("click", refreshRun);
liveNodes.finish?.addEventListener("click", finishRun);
liveNodes.notesForm?.addEventListener("submit", saveNotes);

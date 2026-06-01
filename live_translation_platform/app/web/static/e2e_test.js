const e2eState = { runId: null };

const e2eNodes = {
  form: document.querySelector("#e2eQaForm"),
  lessonId: document.querySelector("#e2eLessonId"),
  title: document.querySelector("#e2eTitle"),
  sttProvider: document.querySelector("#e2eSttProvider"),
  translator: document.querySelector("#e2eTranslator"),
  ttsProvider: document.querySelector("#e2eTtsProvider"),
  ttsLanguage: document.querySelector("#e2eTtsLanguage"),
  actions: document.querySelector("#e2eRunActions"),
  activeRun: document.querySelector("#e2eActiveRun"),
  teacherUrl: document.querySelector("#e2eTeacherUrl"),
  studentUrl: document.querySelector("#e2eStudentUrl"),
  finish: document.querySelector("#e2eFinish"),
  toast: document.querySelector("#toast"),
};

function showToast(message) {
  if (!e2eNodes.toast) return;
  e2eNodes.toast.textContent = message;
  e2eNodes.toast.hidden = false;
  setTimeout(() => { e2eNodes.toast.hidden = true; }, 3500);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

async function startRun(event) {
  event.preventDefault();
  const payload = {
    lesson_id: e2eNodes.lessonId.value,
    title: e2eNodes.title.value,
    stt_provider: e2eNodes.sttProvider.value,
    translation_provider: e2eNodes.translator.value,
    tts_provider: e2eNodes.ttsProvider.value,
    tts_language: e2eNodes.ttsLanguage.value,
  };
  try {
    const created = await readJson(await fetch("/api/e2e-tests", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
    e2eState.runId = created.e2e_test_id;
    e2eNodes.actions.hidden = false;
    e2eNodes.activeRun.textContent = `${created.e2e_test_id} (${created.status})`;
    e2eNodes.teacherUrl.href = created.teacher_url;
    e2eNodes.studentUrl.href = created.student_url;
  } catch (error) {
    showToast(error.message);
  }
}

async function finishRun() {
  if (!e2eState.runId) return;
  try {
    const run = await readJson(await fetch(`/api/e2e-tests/${e2eState.runId}/finish`, { method: "POST" }));
    e2eNodes.activeRun.textContent = `${run.e2e_test_id} (${run.status})`;
  } catch (error) {
    showToast(error.message);
  }
}

e2eNodes.form?.addEventListener("submit", startRun);
e2eNodes.finish?.addEventListener("click", finishRun);

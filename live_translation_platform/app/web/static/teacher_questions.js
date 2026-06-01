const teacherQuestionsPanel = document.querySelector(".teacher-questions-panel");
const teacherQuestionState = {lessonId: teacherQuestionsPanel?.dataset.lessonId, questions: new Map()};
const teacherQuestionNodes = {
  status: document.querySelector("#teacherQuestionStatus"),
  list: document.querySelector("#teacherQuestionList"),
};

function teacherQuestionToken(...names) {
  const params = new URLSearchParams(window.location.search);
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return "";
}

function teacherQuestionWsUrl(path, token) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${window.location.host}${path}`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

function setTeacherQuestionStatus(value) {
  if (teacherQuestionNodes.status) teacherQuestionNodes.status.textContent = value;
}

async function loadTeacherQuestions() {
  if (!teacherQuestionState.lessonId) return;
  const response = await fetch(`/api/lessons/${teacherQuestionState.lessonId}/questions`);
  if (!response.ok) return;
  const questions = await response.json();
  questions.reverse().forEach((question) => upsertTeacherQuestion(question));
}

function upsertTeacherQuestion(question) {
  if (!teacherQuestionNodes.list || !question) return;
  teacherQuestionState.questions.set(question.id, question);
  let item = teacherQuestionNodes.list.querySelector(`[data-question-id="${question.id}"]`);
  if (!item) {
    item = document.createElement("article");
    item.className = "caption-item question-item";
    item.dataset.questionId = question.id;
    item.innerHTML = `
      <div class="card-row"><strong></strong><span></span></div>
      <pre></pre>
      <div class="actions">
        <button class="secondary" data-action="answer" type="button">Mark answered</button>
        <button class="danger" data-action="dismiss" type="button">Dismiss</button>
      </div>
    `;
    item.addEventListener("click", onTeacherQuestionAction);
    teacherQuestionNodes.list.prepend(item);
  }
  item.querySelector("strong").textContent = `${question.student_name || "Student"} / ${question.input_type} / ${question.source_language}`;
  item.querySelector("span").textContent = `${question.status}${question.latency_ms == null ? "" : ` / ${question.latency_ms} ms`}`;
  item.querySelector("pre").textContent = teacherQuestionDisplayText(question);
  item.classList.toggle("new-question", question.status === "new");
}

function teacherQuestionDisplayText(question) {
  if (question.status === "error" || question.error) {
    const metadata = teacherQuestionMetadata(question);
    const error = metadata.error || {};
    const lines = [
      `Error: ${question.error || error.message || "question error"}`,
      `Code: ${error.code || "unknown"}`,
    ];
    const detail = error.detail || metadata.provider_last_error || metadata.error_detail;
    if (detail) lines.push(`Detail: ${detail}`);
    return lines.join("\n");
  }
  const original = question.original_text || question.recognized_text || "";
  const russian = question.translated_text_ru || "";
  if (!original && !russian) return "No transcript available.";
  return `Original: ${original || "not available"}\nRussian: ${russian || "not available"}`;
}

function teacherQuestionMetadata(question) {
  if (!question?.metadata_json) return {};
  try {
    return JSON.parse(question.metadata_json) || {};
  } catch (_) {
    return {};
  }
}

async function onTeacherQuestionAction(event) {
  const action = event.target?.dataset?.action;
  if (!action) return;
  const item = event.currentTarget;
  const questionId = item.dataset.questionId;
  const response = await fetch(`/api/lessons/${teacherQuestionState.lessonId}/questions/${questionId}/${action}`, {method: "POST"});
  if (response.ok) upsertTeacherQuestion(await response.json());
}

function connectTeacherQuestions() {
  if (!teacherQuestionState.lessonId) return;
  const token = teacherQuestionToken("question_token", "teacher_token", "token");
  const socket = new WebSocket(teacherQuestionWsUrl(`/ws/lessons/${teacherQuestionState.lessonId}/questions`, token));
  socket.onopen = () => setTeacherQuestionStatus("connected");
  socket.onclose = (event) => setTeacherQuestionStatus(event.code === 4401 || event.code === 4403 ? "auth required" : "disconnected");
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.question) upsertTeacherQuestion(payload.question);
    if (payload.event === "question_error") setTeacherQuestionStatus(`${payload.code || "question_error"}: ${payload.error || "question error"}`);
  };
}

loadTeacherQuestions();
connectTeacherQuestions();

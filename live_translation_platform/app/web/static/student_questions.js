const studentQuestionsPanel = document.querySelector(".student-questions-panel");
const studentQuestionState = {
  lessonId: studentQuestionsPanel?.dataset.lessonId,
  socket: null,
  stream: null,
  context: null,
  source: null,
  processor: null,
  chunksSent: 0,
  bytesSent: 0,
  startedAt: 0,
  targetSampleRate: 16000,
  chunkMs: 200,
  maxAudioBytes: Number(studentQuestionsPanel?.dataset.maxAudioBytes || 1048576),
  maxDurationSeconds: Number(studentQuestionsPanel?.dataset.maxDurationSeconds || 20),
  pcmBuffer: [],
  pcmBufferFrames: 0,
};

const studentQuestionNodes = {
  status: document.querySelector("#studentQuestionStatus"),
  language: document.querySelector("#studentQuestionLanguage"),
  name: document.querySelector("#studentQuestionName"),
  text: document.querySelector("#studentQuestionText"),
  sendText: document.querySelector("#sendStudentTextQuestion"),
  startVoice: document.querySelector("#startStudentVoiceQuestion"),
  stopVoice: document.querySelector("#stopStudentVoiceQuestion"),
  voiceStatus: document.querySelector("#studentVoiceQuestionStatus"),
  recognized: document.querySelector("#studentQuestionRecognized"),
  list: document.querySelector("#studentQuestionList"),
};

function studentQuestionToken(...names) {
  const params = new URLSearchParams(window.location.search);
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return "";
}

function studentQuestionWsUrl(path, token) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${window.location.host}${path}`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

function setStudentQuestionStatus(value) {
  if (studentQuestionNodes.status) studentQuestionNodes.status.textContent = value;
}

function renderOwnQuestion(question) {
  if (!studentQuestionNodes.list || !question) return;
  const item = document.createElement("article");
  item.className = "caption-item";
  const title = `${question.input_type} / ${question.source_language} / ${question.status}`;
  item.innerHTML = `<div class="card-row"><strong></strong><span></span></div><pre></pre>`;
  item.querySelector("strong").textContent = title;
  item.querySelector("span").textContent = question.latency_ms == null ? "" : `${question.latency_ms} ms`;
  item.querySelector("pre").textContent = questionDisplayText(question);
  studentQuestionNodes.list.prepend(item);
}

function questionDisplayText(question) {
  if (question.status === "error" || question.error) {
    const metadata = questionMetadata(question);
    const error = metadata.error || {};
    const lines = [
      `Error: ${question.error || error.message || "voice question failed"}`,
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

function questionMetadata(question) {
  if (!question?.metadata_json) return {};
  try {
    return JSON.parse(question.metadata_json) || {};
  } catch (_) {
    return {};
  }
}

async function sendStudentTextQuestion() {
  if (!studentQuestionState.lessonId) return;
  const text = studentQuestionNodes.text?.value.trim();
  if (!text) {
    setStudentQuestionStatus("enter a question first");
    return;
  }
  setStudentQuestionStatus("sending text question");
  const token = studentQuestionToken("question_token", "student_token", "token");
  const questionTextUrl = token
    ? `/api/lessons/${studentQuestionState.lessonId}/questions/text?token=${encodeURIComponent(token)}`
    : `/api/lessons/${studentQuestionState.lessonId}/questions/text`;
  const response = await fetch(questionTextUrl, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      student_name: studentQuestionNodes.name?.value || "Student",
      source_language: studentQuestionNodes.language?.value || "kk",
      text,
    }),
  });
  if (!response.ok) {
    setStudentQuestionStatus(await studentQuestionErrorMessage(response));
    return;
  }
  const question = await response.json();
  studentQuestionNodes.text.value = "";
  setStudentQuestionStatus("sent");
  renderOwnQuestion(question);
}

async function startStudentVoiceQuestion() {
  if (!studentQuestionState.lessonId || studentQuestionsPanel?.dataset.audioEnabled !== "true") {
    setStudentQuestionStatus("voice disabled");
    return;
  }
  if (studentQuestionState.socket) return;
  setStudentQuestionStatus("requesting microphone");
  studentQuestionState.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1}, video: false});
  studentQuestionState.context = new AudioContext();
  studentQuestionState.source = studentQuestionState.context.createMediaStreamSource(studentQuestionState.stream);
  studentQuestionState.processor = studentQuestionState.context.createScriptProcessor(2048, 1, 1);
  const token = studentQuestionToken("question_token", "student_token", "token");
  studentQuestionState.socket = new WebSocket(studentQuestionWsUrl(`/ws/lessons/${studentQuestionState.lessonId}/student-question-audio`, token));
  studentQuestionState.socket.binaryType = "arraybuffer";
  studentQuestionState.socket.onopen = () => {
    studentQuestionState.startedAt = Date.now();
    studentQuestionState.bytesSent = 0;
    studentQuestionState.chunksSent = 0;
    studentQuestionState.socket.send(JSON.stringify({
      event: "question_audio_metadata",
      student_name: studentQuestionNodes.name?.value || "Student",
      source_language: studentQuestionNodes.language?.value || "kk",
      sample_rate: studentQuestionState.targetSampleRate,
      channels: 1,
      format: "pcm_s16le",
      chunk_ms: studentQuestionState.chunkMs,
    }));
    studentQuestionState.source.connect(studentQuestionState.processor);
    studentQuestionState.processor.connect(studentQuestionState.context.destination);
    studentQuestionNodes.startVoice.disabled = true;
    studentQuestionNodes.stopVoice.disabled = false;
    setStudentQuestionStatus("recording voice question");
  };
  studentQuestionState.socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.event === "question_created") {
      setStudentQuestionStatus("voice question sent");
      studentQuestionNodes.recognized.textContent = payload.question?.recognized_text || "none";
      renderOwnQuestion(payload.question);
    } else if (payload.event === "question_error") {
      setStudentQuestionStatus(payload.error || payload.code || "voice question failed");
      if (studentQuestionNodes.recognized) studentQuestionNodes.recognized.textContent = payload.code || "error";
      renderOwnQuestion(payload.question);
    }
    cleanupStudentVoice(false);
  };
  studentQuestionState.socket.onclose = () => cleanupStudentVoice(false);
  studentQuestionState.processor.onaudioprocess = (event) => {
    if (!studentQuestionState.socket || studentQuestionState.socket.readyState !== WebSocket.OPEN) return;
    if (studentQuestionState.maxDurationSeconds && Date.now() - studentQuestionState.startedAt > studentQuestionState.maxDurationSeconds * 1000) {
      setStudentQuestionStatus("voice question duration limit reached");
      stopStudentVoiceQuestion();
      return;
    }
    enqueueStudentVoiceChunk(resampleMono(event.inputBuffer.getChannelData(0), studentQuestionState.context.sampleRate, studentQuestionState.targetSampleRate));
    if (studentQuestionNodes.voiceStatus) studentQuestionNodes.voiceStatus.textContent = `${Math.round((Date.now() - studentQuestionState.startedAt) / 1000)}s / chunks ${studentQuestionState.chunksSent}`;
  };
}

function stopStudentVoiceQuestion() {
  if (studentQuestionState.socket?.readyState === WebSocket.OPEN) {
    flushStudentVoiceChunk();
    studentQuestionState.socket.send(JSON.stringify({event: "finish_question"}));
    setStudentQuestionStatus("sending voice question");
  } else {
    cleanupStudentVoice(true);
  }
}

function cleanupStudentVoice(closeSocket) {
  try { studentQuestionState.processor?.disconnect(); } catch (_) {}
  try { studentQuestionState.source?.disconnect(); } catch (_) {}
  studentQuestionState.stream?.getTracks().forEach((track) => track.stop());
  if (closeSocket) studentQuestionState.socket?.close();
  studentQuestionState.socket = null;
  studentQuestionState.stream = null;
  studentQuestionState.context = null;
  studentQuestionState.source = null;
  studentQuestionState.processor = null;
  studentQuestionState.chunksSent = 0;
  studentQuestionState.bytesSent = 0;
  studentQuestionState.pcmBuffer = [];
  studentQuestionState.pcmBufferFrames = 0;
  if (studentQuestionNodes.startVoice) studentQuestionNodes.startVoice.disabled = false;
  if (studentQuestionNodes.stopVoice) studentQuestionNodes.stopVoice.disabled = true;
}

function enqueueStudentVoiceChunk(input) {
  studentQuestionState.pcmBuffer.push(input);
  studentQuestionState.pcmBufferFrames += input.length;
  const targetFrames = Math.floor(studentQuestionState.targetSampleRate * studentQuestionState.chunkMs / 1000);
  while (studentQuestionState.pcmBufferFrames >= targetFrames) {
    const chunk = new Float32Array(targetFrames);
    let offset = 0;
    while (offset < targetFrames && studentQuestionState.pcmBuffer.length) {
      const head = studentQuestionState.pcmBuffer[0];
      const take = Math.min(head.length, targetFrames - offset);
      chunk.set(head.subarray(0, take), offset);
      offset += take;
      if (take === head.length) {
        studentQuestionState.pcmBuffer.shift();
      } else {
        studentQuestionState.pcmBuffer[0] = head.subarray(take);
      }
    }
    studentQuestionState.pcmBufferFrames -= targetFrames;
    const payload = floatToPcm16(chunk);
    if (studentQuestionState.maxAudioBytes && studentQuestionState.bytesSent + payload.byteLength > studentQuestionState.maxAudioBytes) {
      setStudentQuestionStatus("voice question audio limit reached");
      stopStudentVoiceQuestion();
      return;
    }
    studentQuestionState.socket.send(payload);
    studentQuestionState.bytesSent += payload.byteLength;
    studentQuestionState.chunksSent += 1;
  }
}

function flushStudentVoiceChunk() {
  if (!studentQuestionState.pcmBufferFrames || studentQuestionState.socket?.readyState !== WebSocket.OPEN) return;
  const chunk = new Float32Array(studentQuestionState.pcmBufferFrames);
  let offset = 0;
  for (const part of studentQuestionState.pcmBuffer) {
    chunk.set(part, offset);
    offset += part.length;
  }
  const payload = floatToPcm16(chunk);
  studentQuestionState.socket.send(payload);
  studentQuestionState.bytesSent += payload.byteLength;
  studentQuestionState.chunksSent += 1;
  studentQuestionState.pcmBuffer = [];
  studentQuestionState.pcmBufferFrames = 0;
}

function resampleMono(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const output = new Float32Array(Math.max(1, Math.floor(input.length / ratio)));
  for (let i = 0; i < output.length; i += 1) {
    output[i] = input[Math.min(input.length - 1, Math.floor(i * ratio))];
  }
  return output;
}

function floatToPcm16(input) {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < input.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return buffer;
}

studentQuestionNodes.sendText?.addEventListener("click", sendStudentTextQuestion);
studentQuestionNodes.startVoice?.addEventListener("click", () => startStudentVoiceQuestion().catch((error) => setStudentQuestionStatus(error.message || String(error))));
studentQuestionNodes.stopVoice?.addEventListener("click", stopStudentVoiceQuestion);

async function studentQuestionErrorMessage(response) {
  if (response.status === 429) return "Too many requests, please wait.";
  try {
    const payload = await response.json();
    return payload.detail?.message || payload.error?.message || "send failed";
  } catch (_) {
    return "send failed";
  }
}

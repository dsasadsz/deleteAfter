const browserAudioPanel = document.querySelector(".browser-audio-panel");
const browserMicAssetVersion = "browser-audio-debug-20260514";
const micState = {
  lessonId: browserAudioPanel?.dataset.lessonId,
  socket: null,
  stream: null,
  context: null,
  source: null,
  node: null,
  silentGain: null,
  inputSampleRate: 0,
  targetSampleRate: 16000,
  chunkMs: 100,
  commitStrategy: "vad",
  partialsForLive: true,
  manualCommitAfterSilenceMs: 800,
  maxSegmentDurationMs: 5000,
  silenceRmsThreshold: 0.01,
  chunksSent: 0,
  bytesSent: 0,
  lastClientSentAt: 0,
  lastCaptureAt: 0,
  workletMessagesReceived: 0,
  floatFramesReceived: 0,
  pcmFramesSent: 0,
  binaryWsFramesSent: 0,
  lastWorkletAt: 0,
  buffer: [],
  bufferFrames: 0,
  statusTimer: null,
  diagnosticsTimer: null,
  fallbackTimer: null,
  usingWorklet: false,
  captureBackend: "none",
  lastFrontendError: "none",
  fallbackReason: "none",
  frontendInitialized: false,
  startButtonBound: false,
  startClickedCount: 0,
  failedConnectAttempts: 0,
  retryBlockedUntil: 0,
  lessonIdFromDom: browserAudioPanel?.dataset.lessonId || "none",
  audioIngestUrl: "none",
};

const micNodes = {
  start: document.querySelector("#startBrowserMic"),
  stop: document.querySelector("#stopBrowserMic"),
  status: document.querySelector("#browserMicStatus"),
  chunks: document.querySelector("#browserMicChunks"),
  bytes: document.querySelector("#browserMicBytes"),
  dropped: document.querySelector("#browserMicDropped"),
  queue: document.querySelector("#browserMicQueue"),
  sampleRate: document.querySelector("#browserMicSampleRate"),
  chunkMs: document.querySelector("#browserMicChunkMs"),
  uploadLatency: document.querySelector("#browserMicUploadLatency"),
  workletMessages: document.querySelector("#browserMicWorkletMessages"),
  floatFrames: document.querySelector("#browserMicFloatFrames"),
  pcmFramesSent: document.querySelector("#browserMicPcmFramesSent"),
  binaryWsFramesSent: document.querySelector("#browserMicBinaryWsFramesSent"),
  wsReadyState: document.querySelector("#browserMicWsReadyState"),
  audioContextState: document.querySelector("#browserMicAudioContextState"),
  inputSampleRate: document.querySelector("#browserMicInputSampleRate"),
  captureBackend: document.querySelector("#browserMicCaptureBackend"),
  lastFrontendError: document.querySelector("#browserMicLastFrontendError"),
  fallbackReason: document.querySelector("#browserMicFallbackReason"),
  frontendInitialized: document.querySelector("#browserMicFrontendInitialized"),
  startButtonBound: document.querySelector("#browserMicStartButtonBound"),
  startClickedCount: document.querySelector("#browserMicStartClickedCount"),
  lessonIdFromDom: document.querySelector("#browserMicLessonIdFromDom"),
  audioIngestUrl: document.querySelector("#browserMicAudioIngestUrl"),
  trackState: document.querySelector("#browserMicTrackState"),
  backendWs: document.querySelector("#browserMicBackendWs"),
  backendMetadata: document.querySelector("#browserMicBackendMetadata"),
  backendBinaryFrames: document.querySelector("#browserMicBackendBinaryFrames"),
  backendConnection: document.querySelector("#browserMicBackendConnection"),
  error: document.querySelector("#browserMicError"),
  selector: document.querySelector("#audioSourceSelector"),
  saveSource: document.querySelector("#saveAudioSource"),
  lessonSource: document.querySelector("#lessonAudioSource"),
  forceCommit: document.querySelector("#forceSttCommit"),
  commitStrategy: document.querySelector("#sttCommitStrategy"),
  chunkMsInput: document.querySelector("#browserMicChunkMsInput"),
  partialsForLive: document.querySelector("#partialsForLive"),
  manualCommitAfterSilence: document.querySelector("#manualCommitAfterSilence"),
  manualCommitSilenceMs: document.querySelector("#manualCommitSilenceMs"),
  maxSegmentDurationMs: document.querySelector("#maxSegmentDurationMs"),
  commitStrategyValue: document.querySelector("#browserMicCommitStrategy"),
  silenceCommitValue: document.querySelector("#browserMicSilenceCommit"),
  maxSegmentValue: document.querySelector("#browserMicMaxSegment"),
  partialsValue: document.querySelector("#browserMicPartials"),
  pipelineStatus: document.querySelector("#pipelineStatus"),
  pipelineAudioSource: document.querySelector("#pipelineAudioSource"),
  pipelineChunksProcessed: document.querySelector("#pipelineChunksProcessed"),
  sttProviderStatus: document.querySelector("#sttProviderStatus"),
  sttProviderChunksSent: document.querySelector("#sttProviderChunksSent"),
  sttProviderBytesSent: document.querySelector("#sttProviderBytesSent"),
  sttProviderPartials: document.querySelector("#sttProviderPartials"),
  sttProviderFinals: document.querySelector("#sttProviderFinals"),
  sttProviderNoMatch: document.querySelector("#sttProviderNoMatch"),
  sttProviderCanceled: document.querySelector("#sttProviderCanceled"),
  sttProviderLastEvent: document.querySelector("#sttProviderLastEvent"),
  sttProviderLastError: document.querySelector("#sttProviderLastError"),
  sttProviderLastTranscript: document.querySelector("#sttProviderLastTranscript"),
  captionsSent: document.querySelector("#captionsSent"),
  translationRequests: document.querySelector("#translationRequests"),
  translationErrors: document.querySelector("#translationErrors"),
  translationAvgLatency: document.querySelector("#translationAvgLatency"),
  translationLastError: document.querySelector("#translationLastError"),
};

function pageToken(...names) {
  const params = new URLSearchParams(window.location.search);
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return browserAudioPanel?.dataset.audioToken || "";
}

function websocketUrl(path, token) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${window.location.host}${path}`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

function setMicStatus(value) {
  if (micNodes.status) micNodes.status.textContent = value;
}

function setMicError(value) {
  micState.lastFrontendError = value || "none";
  if (micNodes.error) micNodes.error.textContent = value || "none";
  renderFrontendMicDiagnostics();
}

async function startBrowserMic() {
  const now = Date.now();
  if (micState.retryBlockedUntil > now) {
    setMicError(`WebSocket retry paused after repeated failures. Try again in ${Math.ceil((micState.retryBlockedUntil - now) / 1000)}s.`);
    return;
  }
  if (!micState.lessonId) {
    setMicError("Missing lesson id for browser microphone streaming");
    return;
  }
  if (micState.socket) {
    setMicError(`Microphone WebSocket already exists: ${websocketReadyStateName(micState.socket.readyState)}`);
    return;
  }
  try {
    setMicStatus("requesting_permission");
    setMicError("none");
    micState.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    micState.context = new AudioContext();
    if (micState.context.state === "suspended") {
      await micState.context.resume();
    }
    micState.inputSampleRate = micState.context.sampleRate;
    micState.targetSampleRate = 16000;
    applyTuningFromControls();
    micNodes.sampleRate.textContent = `${micState.targetSampleRate}`;
    micState.source = micState.context.createMediaStreamSource(micState.stream);
    micState.silentGain = micState.context.createGain();
    micState.silentGain.gain.value = 0;

    const token = pageToken("teacher_token", "audio_token", "token");
    const path = token ? `/ws/v1/lessons/${micState.lessonId}/audio-ingest` : `/ws/lessons/${micState.lessonId}/audio-ingest`;
    micState.audioIngestUrl = websocketUrl(path, token);
    renderFrontendMicDiagnostics();
    micState.socket = new WebSocket(micState.audioIngestUrl);
    micState.socket.binaryType = "arraybuffer";
    micState.socket.onopen = async () => {
      micState.failedConnectAttempts = 0;
      micState.retryBlockedUntil = 0;
      setMicStatus("connected");
      renderFrontendMicDiagnostics();
      sendMetadata();
      await attachAudioNode();
      micNodes.start.disabled = true;
      micNodes.stop.disabled = false;
      pollBrowserAudioStatus();
      micState.statusTimer = setInterval(pollBrowserAudioStatus, 1500);
    };
    micState.socket.onclose = (event) => {
      if (event.code !== 1000) {
        micState.failedConnectAttempts += 1;
        setMicError(readableWebSocketClose(event));
        if (micState.failedConnectAttempts >= 3) {
          micState.retryBlockedUntil = Date.now() + 30000;
        }
      }
      if (micState.usingWorklet && micState.workletMessagesReceived === 0) {
        micState.fallbackReason = `not_run_ws_closed_before_timeout code=${event.code}`;
      }
      setMicStatus("disconnected");
      renderFrontendMicDiagnostics();
      stopBrowserMic(false);
    };
    micState.socket.onerror = () => {
      setMicError("WebSocket connection failed. Check teacher token, lesson id, and server availability.");
    };
  } catch (error) {
    setMicStatus("error");
    setMicError(readableMicrophoneError(error));
    stopBrowserMic(false);
  }
}

function readableWebSocketClose(event) {
  const reason = event.reason || "";
  if (reason === "WS_TOKEN_MISSING") return "WebSocket auth required: teacher token missing";
  if (reason === "WS_TOKEN_SCOPE_MISSING") return "WebSocket auth failed: teacher token is missing audio:write scope";
  if (reason === "WS_TOKEN_LESSON_MISMATCH") return "WebSocket auth failed: teacher token belongs to another lesson";
  if (reason === "WS_TOKEN_EXPIRED") return "WebSocket auth failed: teacher token expired";
  if (reason === "WS_TOKEN_INVALID") return "WebSocket auth failed: teacher token invalid";
  if (reason === "LESSON_NOT_FOUND") return "Lesson not found";
  if (reason === "AUDIO_INGEST_NOT_AVAILABLE") return "Audio ingest endpoint unavailable";
  if (event.code === 4401) return "WebSocket auth required: teacher token missing";
  if (event.code === 4403) return "WebSocket auth failed: teacher token is not allowed";
  if (event.code === 4404) return "Lesson not found";
  return `WebSocket closed: code=${event.code} reason=${reason || "none"}`;
}

function readableMicrophoneError(error) {
  if (error?.name === "NotAllowedError" || error?.name === "SecurityError") return "Microphone permission denied";
  if (error?.name === "NotFoundError") return "Microphone not found";
  return error?.message || String(error);
}

async function attachAudioNode() {
  if (!micState.context || !micState.source) return;
  if (micState.context.audioWorklet) {
    try {
      await micState.context.audioWorklet.addModule(`/static/audio_worklet_processor.js?v=${browserMicAssetVersion}`);
      micState.node = new AudioWorkletNode(micState.context, "teacher-mic-processor");
      micState.node.port.onmessage = (event) => handleCapturedFloatChunk(event.data);
      micState.source.connect(micState.node);
      micState.node.connect(micState.silentGain);
      micState.silentGain.connect(micState.context.destination);
      micState.usingWorklet = true;
      micState.captureBackend = "audio_worklet";
      micState.fallbackReason = "waiting_for_worklet_message";
      await resumeAudioContext();
      scheduleWorkletFallback();
      return;
    } catch (error) {
      setMicError(`AudioWorklet fallback: ${error.message}`);
    }
  }
  attachScriptProcessorNode();
}

function attachScriptProcessorNode() {
  if (!micState.context || !micState.source || !micState.silentGain) return;
  const processor = micState.context.createScriptProcessor(2048, 1, 1);
  processor.onaudioprocess = (event) => {
    handleCapturedFloatChunk(event.inputBuffer.getChannelData(0));
  };
  micState.node = processor;
  micState.source.connect(processor);
  processor.connect(micState.silentGain);
  micState.silentGain.connect(micState.context.destination);
  micState.usingWorklet = false;
  micState.captureBackend = "script_processor";
  resumeAudioContext();
}

function scheduleWorkletFallback() {
  if (micState.fallbackTimer) clearTimeout(micState.fallbackTimer);
  micState.fallbackTimer = setTimeout(() => {
    if (!micState.usingWorklet || micState.workletMessagesReceived > 0) return;
    micState.fallbackReason = "audio_worklet_no_messages_after_1200ms";
    try {
      micState.node?.disconnect();
      micState.source?.disconnect();
    } catch (_) {}
    attachScriptProcessorNode();
    setMicError("AudioWorklet produced no frames; using ScriptProcessor fallback");
  }, 1200);
}

async function resumeAudioContext() {
  if (micState.context?.state === "suspended") {
    await micState.context.resume();
  }
  renderFrontendMicDiagnostics();
}

function handleCapturedFloatChunk(input) {
  micState.workletMessagesReceived += 1;
  if (micState.captureBackend === "audio_worklet" && micState.fallbackReason === "waiting_for_worklet_message") {
    micState.fallbackReason = "not_needed";
  }
  micState.floatFramesReceived += input?.length || 0;
  micState.lastWorkletAt = Date.now();
  renderFrontendMicDiagnostics();
  enqueueFloatChunk(input);
}

function sendMetadata() {
  if (!micState.socket || micState.socket.readyState !== WebSocket.OPEN) return;
  micState.socket.send(JSON.stringify({
    event: "audio_metadata",
    sample_rate: micState.targetSampleRate,
    channels: 1,
    format: "pcm_s16le",
    chunk_ms: micState.chunkMs,
    source: "browser_mic",
    input_sample_rate: micState.inputSampleRate,
    client_started_at: new Date().toISOString(),
    commit_strategy: micState.commitStrategy,
    partials_for_live: micState.partialsForLive,
    manual_commit_after_silence_ms: micState.manualCommitAfterSilenceMs,
    max_segment_duration_ms: micState.maxSegmentDurationMs,
    periodic_commit_enabled: micState.maxSegmentDurationMs > 0,
    silence_rms_threshold: micState.silenceRmsThreshold,
  }));
}

function sendTuning() {
  if (!micState.socket || micState.socket.readyState !== WebSocket.OPEN) return;
  applyTuningFromControls();
  micState.socket.send(JSON.stringify({
    event: "stt_tuning",
    commit_strategy: micState.commitStrategy,
    chunk_ms: micState.chunkMs,
    partials_for_live: micState.partialsForLive,
    manual_commit_after_silence_ms: micState.manualCommitAfterSilenceMs,
    max_segment_duration_ms: micState.maxSegmentDurationMs,
    periodic_commit_enabled: micState.maxSegmentDurationMs > 0,
    silence_rms_threshold: micState.silenceRmsThreshold,
  }));
}

function applyTuningFromControls() {
  micState.commitStrategy = micNodes.commitStrategy?.value || micState.commitStrategy;
  micState.chunkMs = Number(micNodes.chunkMsInput?.value || micState.chunkMs || 100);
  micState.partialsForLive = Boolean(micNodes.partialsForLive?.checked);
  const silenceEnabled = Boolean(micNodes.manualCommitAfterSilence?.checked);
  const silenceMs = Number(micNodes.manualCommitSilenceMs?.value || 0);
  micState.manualCommitAfterSilenceMs = silenceEnabled ? silenceMs : 0;
  micState.maxSegmentDurationMs = Number(micNodes.maxSegmentDurationMs?.value || micState.maxSegmentDurationMs || 5000);
  renderTuning();
}

function renderTuning() {
  if (micNodes.chunkMs) micNodes.chunkMs.textContent = `${micState.chunkMs}`;
  if (micNodes.commitStrategyValue) micNodes.commitStrategyValue.textContent = micState.commitStrategy;
  if (micNodes.silenceCommitValue) micNodes.silenceCommitValue.textContent = `${micState.manualCommitAfterSilenceMs} ms`;
  if (micNodes.maxSegmentValue) micNodes.maxSegmentValue.textContent = `${micState.maxSegmentDurationMs} ms`;
  if (micNodes.partialsValue) micNodes.partialsValue.textContent = micState.partialsForLive ? "on" : "off";
}

function enqueueFloatChunk(input) {
  if (!input || !input.length || !micState.socket || micState.socket.readyState !== WebSocket.OPEN) return;
  const copied = new Float32Array(input.length);
  copied.set(input);
  micState.buffer.push(copied);
  micState.bufferFrames += copied.length;
  const targetFrames = Math.max(1, Math.round((micState.inputSampleRate * micState.chunkMs) / 1000));
  while (micState.bufferFrames >= targetFrames) {
    const chunk = takeFrames(targetFrames);
    const captureAt = Date.now() - micState.chunkMs;
    const resampled = resampleLinear(chunk, micState.inputSampleRate, micState.targetSampleRate);
    const pcm = floatToPcm16(resampled);
    const clientSentAt = Date.now();
    micState.socket.send(JSON.stringify({
      event: "audio_chunk",
      client_sent_at: clientSentAt,
      audio_ws_sent_at: clientSentAt,
      mic_client_capture_at: captureAt,
      byte_length: pcm.byteLength,
      chunk_ms: micState.chunkMs,
      rms: rmsFloat32(resampled),
    }));
    micState.socket.send(pcm);
    micState.pcmFramesSent += 1;
    micState.binaryWsFramesSent += 1;
    micState.chunksSent += 1;
    micState.bytesSent += pcm.byteLength;
    micState.lastClientSentAt = clientSentAt;
    micState.lastCaptureAt = captureAt;
    renderLocalMicCounters();
  }
}

function takeFrames(frameCount) {
  const output = new Float32Array(frameCount);
  let offset = 0;
  while (offset < frameCount && micState.buffer.length) {
    const head = micState.buffer[0];
    const needed = frameCount - offset;
    if (head.length <= needed) {
      output.set(head, offset);
      offset += head.length;
      micState.buffer.shift();
    } else {
      output.set(head.subarray(0, needed), offset);
      micState.buffer[0] = head.subarray(needed);
      offset += needed;
    }
  }
  micState.bufferFrames -= frameCount;
  return output;
}

function resampleLinear(input, fromRate, toRate) {
  if (!fromRate || fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    const sourceIndex = index * ratio;
    const left = Math.floor(sourceIndex);
    const right = Math.min(input.length - 1, left + 1);
    const fraction = sourceIndex - left;
    output[index] = input[left] + (input[right] - input[left]) * fraction;
  }
  return output;
}

function floatToPcm16(input) {
  const output = new ArrayBuffer(input.length * 2);
  const view = new DataView(output);
  for (let index = 0; index < input.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, input[index]));
    view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return output;
}

function rmsFloat32(input) {
  if (!input || !input.length) return 0;
  let sum = 0;
  for (let index = 0; index < input.length; index += 1) {
    sum += input[index] * input[index];
  }
  return Math.sqrt(sum / input.length);
}

function renderLocalMicCounters() {
  if (micNodes.chunks) micNodes.chunks.textContent = `${micState.chunksSent}`;
  if (micNodes.bytes) micNodes.bytes.textContent = `${micState.bytesSent}`;
  if (micNodes.uploadLatency && micState.lastClientSentAt) {
    micNodes.uploadLatency.textContent = `${Math.max(0, Date.now() - micState.lastClientSentAt)} ms`;
  }
  renderFrontendMicDiagnostics();
}

function renderFrontendMicDiagnostics() {
  if (micNodes.workletMessages) micNodes.workletMessages.textContent = `${micState.workletMessagesReceived}`;
  if (micNodes.floatFrames) micNodes.floatFrames.textContent = `${micState.floatFramesReceived}`;
  if (micNodes.pcmFramesSent) micNodes.pcmFramesSent.textContent = `${micState.pcmFramesSent}`;
  if (micNodes.binaryWsFramesSent) micNodes.binaryWsFramesSent.textContent = `${micState.binaryWsFramesSent}`;
  if (micNodes.wsReadyState) micNodes.wsReadyState.textContent = websocketReadyStateName(micState.socket?.readyState);
  if (micNodes.audioContextState) micNodes.audioContextState.textContent = micState.context?.state || "none";
  if (micNodes.inputSampleRate) micNodes.inputSampleRate.textContent = `${micState.inputSampleRate || 0}`;
  if (micNodes.captureBackend) micNodes.captureBackend.textContent = micState.captureBackend;
  if (micNodes.lastFrontendError) micNodes.lastFrontendError.textContent = micState.lastFrontendError || "none";
  if (micNodes.fallbackReason) micNodes.fallbackReason.textContent = micState.fallbackReason || "none";
  if (micNodes.frontendInitialized) micNodes.frontendInitialized.textContent = `${micState.frontendInitialized}`;
  if (micNodes.startButtonBound) micNodes.startButtonBound.textContent = `${micState.startButtonBound}`;
  if (micNodes.startClickedCount) micNodes.startClickedCount.textContent = `${micState.startClickedCount}`;
  if (micNodes.lessonIdFromDom) micNodes.lessonIdFromDom.textContent = micState.lessonIdFromDom || "none";
  if (micNodes.audioIngestUrl) micNodes.audioIngestUrl.textContent = safeDisplayUrl(micState.audioIngestUrl);
  if (micNodes.trackState) micNodes.trackState.textContent = trackStateSummary();
}

function websocketReadyStateName(value) {
  if (value === WebSocket.CONNECTING) return "connecting";
  if (value === WebSocket.OPEN) return "open";
  if (value === WebSocket.CLOSING) return "closing";
  if (value === WebSocket.CLOSED) return "closed";
  return "not_created";
}

function safeDisplayUrl(value) {
  if (!value || value === "none") return "none";
  try {
    const url = new URL(value);
    if (url.searchParams.has("token")) url.searchParams.set("token", "...");
    return url.toString();
  } catch (_) {
    return value.includes("token=") ? value.replace(/token=[^&]+/, "token=...") : value;
  }
}

function trackStateSummary() {
  const track = micState.stream?.getAudioTracks?.()[0];
  if (!track) return "none";
  return `${track.readyState} enabled=${track.enabled} muted=${track.muted}`;
}

async function pollBrowserAudioStatus() {
  if (!micState.lessonId) return;
  const response = await fetch(`/api/lessons/${micState.lessonId}/browser-audio`);
  if (!response.ok) return;
  const payload = await response.json();
  if (micNodes.status) micNodes.status.textContent = payload.status;
  if (micNodes.chunks) micNodes.chunks.textContent = payload.chunks_received;
  if (micNodes.bytes) micNodes.bytes.textContent = payload.bytes_received;
  if (micNodes.dropped) micNodes.dropped.textContent = payload.chunks_dropped;
  if (micNodes.queue) micNodes.queue.textContent = payload.queue_size;
  if (micNodes.sampleRate) micNodes.sampleRate.textContent = payload.metadata?.sample_rate || payload.config?.expected_sample_rate || 16000;
  if (micNodes.chunkMs) micNodes.chunkMs.textContent = payload.metadata?.chunk_ms || payload.config?.chunk_ms || 100;
  if (micNodes.commitStrategyValue) micNodes.commitStrategyValue.textContent = payload.metadata?.commit_strategy || payload.config?.commit_strategy || micState.commitStrategy;
  if (micNodes.silenceCommitValue) micNodes.silenceCommitValue.textContent = `${payload.metadata?.manual_commit_after_silence_ms ?? payload.config?.manual_commit_after_silence_ms ?? micState.manualCommitAfterSilenceMs} ms`;
  if (micNodes.maxSegmentValue) micNodes.maxSegmentValue.textContent = `${payload.metadata?.max_segment_duration_ms ?? payload.tuning?.max_segment_duration_ms ?? payload.config?.max_segment_duration_ms ?? micState.maxSegmentDurationMs} ms`;
  if (micNodes.partialsValue) micNodes.partialsValue.textContent = (payload.metadata?.partials_for_live ?? payload.config?.partials_for_live ?? micState.partialsForLive) ? "on" : "off";
  if (micNodes.backendWs) micNodes.backendWs.textContent = `${payload.ws_connected ?? false}`;
  if (micNodes.backendMetadata) micNodes.backendMetadata.textContent = `${payload.metadata_received ?? false}`;
  if (micNodes.backendBinaryFrames) micNodes.backendBinaryFrames.textContent = `${payload.binary_frames_received ?? 0}`;
  if (micNodes.backendConnection) micNodes.backendConnection.textContent = payload.active_connection_id || payload.latest_connection_id || "none";
  if (micNodes.error) micNodes.error.textContent = payload.last_error || "none";
  renderFrontendMicDiagnostics();
}

async function pollLessonDiagnostics() {
  if (!micState.lessonId) return;
  const response = await fetch(`/api/lessons/${micState.lessonId}/diagnostics`);
  if (!response.ok) return;
  const payload = await response.json();
  const lesson = payload.lesson || {};
  if (micNodes.pipelineStatus) micNodes.pipelineStatus.textContent = payload.pipeline?.status || lesson.pipeline_status || "unknown";
  if (micNodes.pipelineAudioSource) micNodes.pipelineAudioSource.textContent = payload.pipeline?.source || lesson.pipeline_audio_source || "none";
  if (micNodes.pipelineChunksProcessed) micNodes.pipelineChunksProcessed.textContent = payload.pipeline?.chunks_processed ?? lesson.pipeline_chunks_processed ?? 0;
  if (micNodes.sttProviderStatus) micNodes.sttProviderStatus.textContent = `${lesson.stt_provider || payload.stt?.provider || "unknown"} / ${lesson.stt_provider_status || "unknown"}`;
  if (micNodes.sttProviderChunksSent) micNodes.sttProviderChunksSent.textContent = lesson.stt_provider_audio_chunks_sent ?? 0;
  if (micNodes.sttProviderBytesSent) micNodes.sttProviderBytesSent.textContent = lesson.stt_provider_audio_bytes_sent ?? 0;
  if (micNodes.sttProviderPartials) micNodes.sttProviderPartials.textContent = payload.stt?.partial_events ?? lesson.stt_provider_partial_events ?? 0;
  if (micNodes.sttProviderFinals) micNodes.sttProviderFinals.textContent = payload.stt?.final_events ?? lesson.stt_provider_final_events ?? 0;
  if (micNodes.sttProviderNoMatch) micNodes.sttProviderNoMatch.textContent = lesson.stt_provider_no_match_count ?? 0;
  if (micNodes.sttProviderCanceled) micNodes.sttProviderCanceled.textContent = lesson.stt_provider_canceled_count ?? 0;
  if (micNodes.sttProviderLastEvent) micNodes.sttProviderLastEvent.textContent = lesson.stt_provider_last_event_at || "none";
  if (micNodes.sttProviderLastError) micNodes.sttProviderLastError.textContent = payload.stt?.last_error || lesson.stt_provider_last_error || "none";
  if (micNodes.sttProviderLastTranscript) micNodes.sttProviderLastTranscript.textContent = lesson.stt_provider_last_transcript || "none";
  if (micNodes.captionsSent) micNodes.captionsSent.textContent = payload.captions?.sent ?? lesson.captions_sent ?? 0;
  if (micNodes.translationRequests) micNodes.translationRequests.textContent = payload.translation?.requests ?? lesson.translation_requests_count ?? 0;
  if (micNodes.translationErrors) micNodes.translationErrors.textContent = payload.translation?.errors ?? lesson.translation_errors_count ?? 0;
  if (micNodes.translationAvgLatency) micNodes.translationAvgLatency.textContent = `${lesson.translation_avg_latency_ms ?? 0} ms`;
  if (micNodes.translationLastError) micNodes.translationLastError.textContent = payload.translation?.last_error || lesson.translation_last_error || "none";
}

function stopBrowserMic(closeSocket = true) {
  if (micState.statusTimer) clearInterval(micState.statusTimer);
  micState.statusTimer = null;
  if (micState.fallbackTimer) clearTimeout(micState.fallbackTimer);
  micState.fallbackTimer = null;
  if (micState.node) {
    try { micState.node.disconnect(); } catch (_) {}
  }
  if (micState.source) {
    try { micState.source.disconnect(); } catch (_) {}
  }
  if (micState.silentGain) {
    try { micState.silentGain.disconnect(); } catch (_) {}
  }
  if (micState.stream) {
    micState.stream.getTracks().forEach((track) => track.stop());
  }
  if (micState.context) {
    micState.context.close().catch(() => {});
  }
  if (closeSocket && micState.socket) {
    micState.socket.close();
  }
  micState.socket = null;
  micState.stream = null;
  micState.context = null;
  micState.source = null;
  micState.node = null;
  micState.silentGain = null;
  micState.buffer = [];
  micState.bufferFrames = 0;
  micState.usingWorklet = false;
  micState.captureBackend = "none";
  renderFrontendMicDiagnostics();
  if (micNodes.start) micNodes.start.disabled = false;
  if (micNodes.stop) micNodes.stop.disabled = true;
}

async function saveAudioSource() {
  if (!micState.lessonId || !micNodes.selector) return;
  const response = await fetch(`/api/lessons/${micState.lessonId}/set-audio-source`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ audio_source: micNodes.selector.value }),
  });
  const payload = await response.json();
  if (!response.ok) {
    setMicError(payload.detail || "Could not set audio source");
    return;
  }
  if (micNodes.lessonSource) micNodes.lessonSource.textContent = payload.audio_source;
  setMicStatus(payload.browser_audio_status || "not_connected");
}

function forceSttCommit() {
  if (!micState.socket || micState.socket.readyState !== WebSocket.OPEN) {
    setMicError("Microphone WebSocket is not connected");
    return;
  }
  micState.socket.send(JSON.stringify({ event: "force_commit", reason: "teacher_force_commit" }));
  setMicStatus("force_commit_sent");
}

function initBrowserMicFrontend() {
  micState.frontendInitialized = true;
  micState.lessonIdFromDom = browserAudioPanel?.dataset.lessonId || "none";
  if (micNodes.start) {
    micNodes.start.addEventListener("click", () => {
      micState.startClickedCount += 1;
      renderFrontendMicDiagnostics();
      startBrowserMic();
    });
    micState.startButtonBound = true;
  } else {
    setMicError("Start microphone button #startBrowserMic not found");
  }
  micNodes.stop?.addEventListener("click", () => stopBrowserMic(true));
  micNodes.saveSource?.addEventListener("click", saveAudioSource);
  micNodes.forceCommit?.addEventListener("click", forceSttCommit);
  micNodes.commitStrategy?.addEventListener("change", sendTuning);
  micNodes.chunkMsInput?.addEventListener("change", sendTuning);
  micNodes.partialsForLive?.addEventListener("change", sendTuning);
  micNodes.manualCommitAfterSilence?.addEventListener("change", sendTuning);
  micNodes.manualCommitSilenceMs?.addEventListener("change", sendTuning);
  micNodes.maxSegmentDurationMs?.addEventListener("change", sendTuning);
  applyTuningFromControls();
  pollBrowserAudioStatus();
  pollLessonDiagnostics();
  micState.diagnosticsTimer = setInterval(pollLessonDiagnostics, 2000);
  renderFrontendMicDiagnostics();
}

if (browserAudioPanel) {
  initBrowserMicFrontend();
}

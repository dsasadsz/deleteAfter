const localLoadState = { runId: null, ws: null };

const localLoadNodes = {
  form: document.querySelector("#localLoadForm"),
  integrationKey: document.querySelector("#localLoadIntegrationKey"),
  sessions: document.querySelector("#localLoadSessions"),
  students: document.querySelector("#localLoadStudents"),
  mode: document.querySelector("#localLoadMode"),
  audio: document.querySelector("#localLoadAudio"),
  reference: document.querySelector("#localLoadReference"),
  targets: document.querySelector("#localLoadTargets"),
  ttsLanguages: document.querySelector("#localLoadTtsLanguages"),
  ttsRatio: document.querySelector("#localLoadTtsRatio"),
  duration: document.querySelector("#localLoadDuration"),
  stop: document.querySelector("#localLoadStop"),
  refresh: document.querySelector("#localLoadRefresh"),
  runId: document.querySelector("#localLoadRunId"),
  status: document.querySelector("#localLoadStatus"),
  connected: document.querySelector("#localLoadConnected"),
  captions: document.querySelector("#localLoadCaptions"),
  p95: document.querySelector("#localLoadP95"),
  ttsHit: document.querySelector("#localLoadTtsHit"),
  logs: document.querySelector("#localLoadLogs"),
  reportLinks: document.querySelector("#localLoadReportLinks"),
  reportPreview: document.querySelector("#localLoadReportPreview"),
  runs: document.querySelector("#localLoadRuns"),
  toast: document.querySelector("#toast"),
};

function showToast(message) {
  if (!localLoadNodes.toast) return;
  localLoadNodes.toast.textContent = message;
  localLoadNodes.toast.hidden = false;
  setTimeout(() => { localLoadNodes.toast.hidden = true; }, 3500);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

function authHeaders() {
  const key = localLoadNodes.integrationKey.value.trim();
  return key ? { "x-integration-key": key } : {};
}

function csv(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function uploadAudioIfNeeded() {
  const file = localLoadNodes.audio.files && localLoadNodes.audio.files[0];
  if (!file) return null;
  const form = new FormData();
  form.append("file", file);
  const uploaded = await readJson(await fetch("/api/load-tests/local/audio", { method: "POST", headers: authHeaders(), body: form }));
  return uploaded.audio_file_id;
}

async function startRun(event) {
  event.preventDefault();
  try {
    const audioFileId = await uploadAudioIfNeeded();
    const payload = {
      sessions: Number(localLoadNodes.sessions.value),
      students_per_session: Number(localLoadNodes.students.value),
      mode: localLoadNodes.mode.value,
      audio_file_id: audioFileId,
      reference_ru_text: localLoadNodes.reference.value,
      target_languages: csv(localLoadNodes.targets.value),
      tts_enabled: true,
      tts_languages: csv(localLoadNodes.ttsLanguages.value),
      tts_request_ratio: Number(localLoadNodes.ttsRatio.value),
      duration_limit_seconds: Number(localLoadNodes.duration.value),
    };
    const created = await readJson(await fetch("/api/load-tests/local", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(payload),
    }));
    localLoadState.runId = created.run_id;
    localLoadNodes.stop.disabled = false;
    renderRun(created);
    openRunSocket(created.run_id);
  } catch (error) {
    showToast(error.message);
  }
}

function openRunSocket(runId) {
  if (localLoadState.ws) localLoadState.ws.close();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  localLoadState.ws = new WebSocket(`${protocol}//${window.location.host}/ws/load-tests/local/${runId}`);
  localLoadState.ws.addEventListener("open", () => localLoadState.ws.send("poll"));
  localLoadState.ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.run) renderRun(payload.run);
    if (localLoadState.ws && localLoadState.ws.readyState === WebSocket.OPEN) {
      setTimeout(() => localLoadState.ws.send("poll"), 1000);
    }
  });
}

async function stopRun() {
  if (!localLoadState.runId) return;
  try {
    const stopped = await readJson(await fetch(`/api/load-tests/local/${localLoadState.runId}/stop`, { method: "POST", headers: authHeaders() }));
    renderRun(stopped);
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshRuns() {
  try {
    const payload = await readJson(await fetch("/api/load-tests/local"));
    localLoadNodes.runs.innerHTML = payload.items.map((run) => `<div class="caption-item"><strong>${run.run_id}</strong><p>${run.status} / ${run.request.mode} / ${run.request.sessions} x ${run.request.students_per_session}</p></div>`).join("") || '<p class="muted">No local virtual load tests yet.</p>';
    if (localLoadState.runId) {
      const run = await readJson(await fetch(`/api/load-tests/local/${localLoadState.runId}`));
      renderRun(run);
    }
  } catch (error) {
    showToast(error.message);
  }
}

function renderRun(run) {
  localLoadState.runId = run.run_id;
  localLoadNodes.runId.textContent = run.run_id;
  localLoadNodes.status.textContent = run.status;
  const connected = (run.students || []).filter((student) => student.connected).length;
  const captions = (run.caption_events || []).length;
  localLoadNodes.connected.textContent = `${connected}/${(run.students || []).length}`;
  localLoadNodes.captions.textContent = captions;
  localLoadNodes.logs.textContent = (run.logs || []).map((item) => `${item.ts || ""} ${item.level || "info"} ${item.message || ""}`).join("\n") || "No logs.";
  const report = run.report || {};
  localLoadNodes.reportPreview.textContent = JSON.stringify(report, null, 2);
  localLoadNodes.reportLinks.innerHTML = run.report_links ? `
    <a class="button secondary" href="${run.report_links.json}" target="_blank">JSON</a>
    <a class="button secondary" href="${run.report_links.markdown}" target="_blank">Markdown</a>
    <a class="button secondary" href="${run.report_links.html}" target="_blank">HTML</a>
  ` : "";
  loadReportStats(run.run_id);
}

async function loadReportStats(runId) {
  try {
    const report = await readJson(await fetch(`/api/load-tests/local/${runId}/report/json`));
    localLoadNodes.p95.textContent = report.latency?.student_receive_latency_ms?.p95 ?? "n/a";
    localLoadNodes.ttsHit.textContent = report.tts?.events ? `${Math.round(report.tts.cache_hit_ratio * 100)}%` : "n/a";
  } catch (_error) {
    localLoadNodes.p95.textContent = "n/a";
    localLoadNodes.ttsHit.textContent = "n/a";
  }
}

localLoadNodes.form?.addEventListener("submit", startRun);
localLoadNodes.stop?.addEventListener("click", stopRun);
localLoadNodes.refresh?.addEventListener("click", refreshRuns);

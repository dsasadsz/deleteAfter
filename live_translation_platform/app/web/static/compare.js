const compareForm = document.querySelector("#compareForm");
const compareAudioMode = document.querySelector("#compareAudioMode");
const compareUploadField = document.querySelector("#compareUploadField");
const compareAudioFile = document.querySelector("#compareAudioFile");
const compareUploadStatus = document.querySelector("#compareUploadStatus");
const compareLog = document.querySelector("#compareLog");
const compareResults = document.querySelector("#compareResults");
const toast = document.querySelector("#toast");
let compareSocket = null;

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  setTimeout(() => {
    toast.hidden = true;
  }, 4500);
}

function logCompare(payload) {
  if (!compareLog) return;
  const line = document.createElement("p");
  line.textContent = `[${payload.event}] ${payload.stt_provider || ""} ${payload.status || payload.reason || payload.error || ""}`;
  compareLog.prepend(line);
}

async function uploadCompareAudioIfNeeded() {
  if (compareAudioMode.value !== "wav_upload") return null;
  if (!compareAudioFile.files.length) throw new Error("Choose a WAV file first.");
  const data = new FormData();
  data.append("file", compareAudioFile.files[0]);
  const response = await fetch("/api/smoke/upload-audio", { method: "POST", body: data });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Audio upload failed.");
  compareUploadStatus.textContent = payload.warning || `Uploaded ${payload.chunks} chunks at ${payload.sample_rate} Hz.`;
  return payload.audio_sample_id;
}

function selectedSttProviders() {
  return [...document.querySelectorAll('input[name="stt_provider"]:checked')].map((node) => node.value);
}

function connectCompareSocket(comparisonId) {
  if (compareSocket) compareSocket.close();
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  compareSocket = new WebSocket(`${protocol}://${window.location.host}/ws/compare/${comparisonId}`);
  compareSocket.onmessage = async (event) => {
    const payload = JSON.parse(event.data);
    logCompare(payload);
    if (payload.event === "provider_completed" || payload.event === "provider_error" || payload.event === "comparison_completed") {
      await refreshComparison(comparisonId);
    }
  };
}

async function refreshComparison(comparisonId) {
  const response = await fetch(`/api/compare/${comparisonId}`);
  if (!response.ok) return;
  const payload = await response.json();
  renderResults(payload.results || [], payload.skipped || [], payload.translation_provider);
}

function renderResults(results, skipped, translationProvider) {
  if (!compareResults) return;
  compareResults.innerHTML = "";
  for (const result of results) {
    appendRow(result, result.translation_provider || translationProvider);
  }
  for (const item of skipped) {
    appendRow({
      stt_provider: item.stt_provider,
      status: "skipped",
      latency_ms: {},
      translations: {},
      original_text: "",
      error: item.reason,
    }, translationProvider);
  }
}

function appendRow(result, translator) {
  const latency = result.latency_ms || {};
  const translations = result.translations || {};
  const row = document.createElement("tr");
  row.innerHTML = `
    <td>${escapeHtml(result.stt_provider || "")}</td>
    <td>${escapeHtml(translator || "")}</td>
    <td>${escapeHtml(result.audio_source || "")}</td>
    <td>${escapeHtml(result.status || "")}</td>
    <td>${latency.ingest_latency_ms ?? 0}</td>
    <td>${latency.first_partial ?? 0}</td>
    <td>${latency.stt_final ?? 0}</td>
    <td>${latency.translation ?? 0}</td>
    <td>${latency.total_server ?? 0}</td>
    <td>${latency.client_receive ?? 0}</td>
    <td>${result.dropped_chunks ?? 0}</td>
    <td>${result.sample_rate ?? ""}</td>
    <td>${escapeHtml(result.estimated_cost ?? "")}</td>
    <td>${escapeHtml(result.original_text || "")}</td>
    <td>${escapeHtml(glossarySummary(result.glossary))}</td>
    <td>${escapeHtml(translations.kk || "")}</td>
    <td>${escapeHtml(translations.uz || "")}</td>
    <td>${escapeHtml(translations["zh-Hans"] || "")}</td>
    <td>${escapeHtml(result.error || "")}</td>
  `;
  compareResults.appendChild(row);
}

function glossarySummary(glossary) {
  if (!glossary?.enabled) return "off";
  const normalization = glossary.normalization_changes?.length || 0;
  const postprocess = glossary.postprocess_changes?.length || 0;
  return `${normalization} normalize / ${postprocess} post`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

compareAudioMode?.addEventListener("change", () => {
  compareUploadField.hidden = compareAudioMode.value !== "wav_upload";
});

compareForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const providers = selectedSttProviders();
    if (!providers.length) throw new Error("Choose at least one STT provider.");
    const audioSampleId = await uploadCompareAudioIfNeeded();
    const response = await fetch("/api/compare/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audio_mode: compareAudioMode.value,
        audio_sample_id: audioSampleId,
        stt_providers: providers,
        translation_provider: document.querySelector("#compareTranslator").value,
        target_languages: document.querySelector("#compareLanguages").value.split(",").map((item) => item.trim()).filter(Boolean),
        run_mode: document.querySelector("#compareRunMode").value,
        glossary_id: document.querySelector("#compareGlossary")?.value || null,
        glossary_enabled: Boolean(document.querySelector("#compareGlossaryEnabled")?.checked && document.querySelector("#compareGlossary")?.value),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Comparison failed to start.");
    renderResults([], payload.skipped || [], document.querySelector("#compareTranslator").value);
    connectCompareSocket(payload.comparison_id);
    await refreshComparison(payload.comparison_id);
  } catch (error) {
    showToast(error.message);
  }
});

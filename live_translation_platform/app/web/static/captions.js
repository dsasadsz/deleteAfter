const root = document.querySelector(".student-layout");
const lessonId = root?.dataset.lessonId;
const selector = document.querySelector("#languageSelector");
const partialCaption = document.querySelector("#partialCaption");
const finalCaptions = document.querySelector("#finalCaptions");
const showCaptionHistory = document.querySelector("#showCaptionHistory");
const visibleCaptionCount = document.querySelector("#visibleCaptionCount");
const overlay = document.querySelector("#captionOverlay");
const captionBelow = document.querySelector("#captionBelow");
const captionBelowText = document.querySelector("#captionBelowText");
const captionPlacement = document.querySelector("#captionPlacement");
const statusBadge = document.querySelector("#connectionStatus");
const totalLatency = document.querySelector("#totalLatency");
const lagMeter = document.querySelector("#lagMeter");
const rtmsDebugBadge = document.querySelector("#rtmsDebugBadge");
const captionSourceBadge = document.querySelector("#captionSourceBadge");
const glossaryDebugBadge = document.querySelector("#glossaryDebugBadge");
const captionLatencyBreakdown = document.querySelector("#captionLatencyBreakdown");
let selectedLanguage = "all";
let selectedPlacement = "overlay";
let captionHistoryExpanded = false;
let latestCaptionPayload = null;
let latestPartialPayload = null;
const MAX_VISIBLE_FINAL_CAPTIONS = 8;
const FINAL_CAPTION_DEDUPE_TTL_MS = 8000;
const FINAL_CAPTION_DEDUPE_MAX_ITEMS = 200;
const TARGET_TRANSLATION_LANGUAGES = ["kk", "uz", "zh-Hans"];
const recentlySeenFinalCaptions = new Map();
window.CaptionDebug = window.CaptionDebug || {
  captions_ws_connections_created: 0,
  captions_events_received: 0,
  duplicate_captions_skipped: 0,
  translation_status_updates: 0,
  last_translation_status: "",
};

function captionPageToken(...names) {
  const params = new URLSearchParams(window.location.search);
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return "";
}

function captionWebSocketUrl(path, token) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${window.location.host}${path}`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

window.CaptionState = {
  selectedLanguage: () => selectedLanguage,
};

showCaptionHistory?.addEventListener("click", () => {
  captionHistoryExpanded = !captionHistoryExpanded;
  applyFinalCaptionVisibility();
});

selector?.addEventListener("change", (event) => {
  selectedLanguage = event.target.value;
  document.querySelectorAll(".caption-item").forEach((item) => {
    if (item.captionPayload) {
      const result = resolveCaptionForLanguage(item.captionPayload, selectedLanguage);
      item.querySelector("pre").textContent = result.text;
      const warning = item.querySelector(".caption-warning");
      if (warning) {
        warning.textContent = result.statusText || "";
        warning.hidden = !result.statusText;
      }
      if (!result.isRenderableCaption) showTranslationStatus(result);
    }
    renderVisibility(item);
  });
  applyFinalCaptionVisibility();
  const activePayload = latestPartialPayload || latestCaptionPayload;
  if (activePayload) {
    const result = resolveCaptionForLanguage(activePayload, selectedLanguage);
    const text = result.text;
    overlay.textContent = text;
    if (captionBelowText) captionBelowText.textContent = text;
    if (latestPartialPayload) partialCaption.textContent = text;
    if (!result.isRenderableCaption) showTranslationStatus(result);
  }
});

captionPlacement?.addEventListener("change", (event) => {
  selectedPlacement = event.target.value;
  if (captionBelow) captionBelow.hidden = selectedPlacement !== "below";
  if (overlay) overlay.hidden = selectedPlacement === "below";
});

function originalCaptionText(payload) {
  return payload.original_text_normalized || payload.original_text_raw || payload.original_text || "";
}

function isUnavailableTranslationText(text) {
  const normalized = String(text || "").trim().toLowerCase();
  return (
    !normalized ||
    normalized.startsWith("translation unavailable") ||
    normalized.startsWith("waiting for ") ||
    normalized.startsWith("перевод временно недоступен") ||
    normalized.startsWith("перевод пока недоступен")
  );
}

function captionResult(kind, text, language, isRenderableCaption, isTtsEligible, extra = {}) {
  return {kind, text, language, isRenderableCaption, isTtsEligible, ...extra};
}

function resolveCaptionForLanguage(payload, language) {
  const originalText = originalCaptionText(payload);
  if (language === "ru" || language === "original") {
    return captionResult("text", originalText, "ru", Boolean(originalText), Boolean(originalText));
  }
  if (language === "all") {
    const lines = [`RU: ${originalText}`];
    const missingLanguages = [];
    for (const translationLanguage of TARGET_TRANSLATION_LANGUAGES) {
      const text = payload.translations?.[translationLanguage];
      if (isUnavailableTranslationText(text)) {
        missingLanguages.push(translationLanguage);
      } else {
        lines.push(`${translationLanguage}: ${text}`);
      }
    }
    return captionResult("all", lines.join("\n"), "all", true, false, {
      missingLanguages,
      statusText: missingLanguages.length ? `Missing translations: ${missingLanguages.join(", ")}` : "",
    });
  }
  const translation = payload.translations?.[language];
  if (translation && !isUnavailableTranslationText(translation)) {
    return captionResult("text", translation, language, true, true);
  }
  if (payload.is_partial) {
    return captionResult("waiting", `Waiting for ${language} translation...`, language, false, false);
  }
  return captionResult(translation ? "error" : "missing", `Translation unavailable for ${language}`, language, false, false);
}

function captionTextForLanguage(payload, language) {
  return resolveCaptionForLanguage(payload, language).text;
}

function ttsTextForLanguage(payload, language) {
  if (!payload || payload.is_partial) return null;
  if (language === "all") return null;
  const result = resolveCaptionForLanguage(payload, language);
  if (!result.isTtsEligible || isUnavailableTranslationText(result.text)) return null;
  return {text: result.text, language: result.language === "original" ? "ru" : result.language};
}

function renderText(payload) {
  return captionTextForLanguage(payload, selectedLanguage);
}

window.CaptionRendering = {
  captionTextForLanguage,
  resolveCaptionForLanguage,
  ttsTextForLanguage,
  isUnavailableTranslationText,
};

function renderVisibility(item) {
  if (!item.captionPayload) {
    item.hidden = false;
    return;
  }
  item.dataset.languageRenderable = resolveCaptionForLanguage(item.captionPayload, selectedLanguage).isRenderableCaption ? "true" : "false";
  applyFinalCaptionVisibility();
}

function finalCaptionItems() {
  if (!finalCaptions) return [];
  if (typeof finalCaptions.querySelectorAll === "function") {
    return Array.from(finalCaptions.querySelectorAll(".final-caption-item"));
  }
  return Array.from(document.querySelectorAll(".caption-item")).filter((item) => item.captionPayload);
}

function applyFinalCaptionVisibility() {
  const items = finalCaptionItems();
  const renderableItems = items.filter((item) => item.dataset.languageRenderable !== "false");
  let renderableIndex = 0;
  for (const item of items) {
    if (item.dataset.languageRenderable === "false") {
      item.hidden = true;
      item.dataset.historyHidden = "false";
      continue;
    }
    renderableIndex += 1;
    const hiddenByHistory = !captionHistoryExpanded && renderableIndex > MAX_VISIBLE_FINAL_CAPTIONS;
    item.hidden = hiddenByHistory;
    item.dataset.historyHidden = hiddenByHistory ? "true" : "false";
  }
  const total = renderableItems.length;
  const visible = captionHistoryExpanded ? total : Math.min(total, MAX_VISIBLE_FINAL_CAPTIONS);
  if (visibleCaptionCount) {
    visibleCaptionCount.textContent = total > MAX_VISIBLE_FINAL_CAPTIONS ? `${visible} of ${total} shown` : `${total} shown`;
  }
  if (showCaptionHistory) {
    const hiddenCount = Math.max(0, total - MAX_VISIBLE_FINAL_CAPTIONS);
    showCaptionHistory.hidden = hiddenCount === 0;
    showCaptionHistory.textContent = captionHistoryExpanded ? "Show latest" : `Show history (${hiddenCount})`;
  }
}

function showTranslationStatus(result) {
  if (result.isRenderableCaption) return;
  window.CaptionDebug.translation_status_updates += 1;
  window.CaptionDebug.last_translation_status = result.text;
  if (partialCaption) partialCaption.textContent = result.text;
}

function pruneRecentlySeenFinalCaptions(now = Date.now()) {
  for (const [key, seenAt] of recentlySeenFinalCaptions.entries()) {
    if (now - seenAt > FINAL_CAPTION_DEDUPE_TTL_MS) recentlySeenFinalCaptions.delete(key);
  }
  while (recentlySeenFinalCaptions.size > FINAL_CAPTION_DEDUPE_MAX_ITEMS) {
    const oldestKey = recentlySeenFinalCaptions.keys().next().value;
    recentlySeenFinalCaptions.delete(oldestKey);
  }
}

function finalCaptionDedupeKey(payload) {
  if (payload.caption_id) return `caption_id:${payload.caption_id}`;
  const speaker = payload.speaker?.id || payload.speaker?.name || "unknown";
  const text = captionTextForLanguage(payload, selectedLanguage).trim().replace(/\s+/g, " ");
  return [payload.lesson_id || lessonId || "", speaker, selectedLanguage, text, Boolean(payload.is_final)].join(":");
}

function shouldRenderFinalCaption(payload) {
  if (!payload?.is_final) return true;
  const now = Date.now();
  pruneRecentlySeenFinalCaptions(now);
  const key = finalCaptionDedupeKey(payload);
  if (recentlySeenFinalCaptions.has(key)) {
    window.CaptionDebug.duplicate_captions_skipped += 1;
    return false;
  }
  recentlySeenFinalCaptions.set(key, now);
  pruneRecentlySeenFinalCaptions(now);
  return true;
}

function addFinalCaption(payload) {
  const result = resolveCaptionForLanguage(payload, selectedLanguage);
  if (!result.isRenderableCaption) {
    showTranslationStatus(result);
    return false;
  }
  if (!shouldRenderFinalCaption(payload)) return false;
  const item = document.createElement("article");
  item.className = "caption-item final-caption-item";
  item.dataset.language = "all";
  if (payload.caption_id) item.dataset.captionId = payload.caption_id;
  if (payload.sequence != null) item.dataset.sequence = String(payload.sequence);
  item.captionPayload = payload;
  const lagClass = payload.latency_ms.total > 1200 ? "late" : "ok";
  item.innerHTML = `
    <div class="card-row">
      <strong>${payload.speaker.name}</strong>
      <span class="${lagClass}">${payload.latency_ms.total} ms</span>
    </div>
    <pre></pre>
    <small class="caption-warning" hidden></small>
  `;
  item.querySelector("pre").textContent = result.text;
  const warning = item.querySelector(".caption-warning");
  if (warning && result.statusText) {
    warning.textContent = result.statusText;
    warning.hidden = false;
  }
  finalCaptions.prepend(item);
  item.dataset.languageRenderable = "true";
  applyFinalCaptionVisibility();
  return true;
}

function updateLatency(payload) {
  const serverLatency = payload.latency_ms.total ?? payload.latency_ms.total_server ?? 0;
  const sentAt = payload.timestamps?.websocket_sent_at ? Date.parse(payload.timestamps.websocket_sent_at) : null;
  const clientReceive = sentAt ? Math.max(0, Date.now() - sentAt) : 0;
  payload.timestamps = payload.timestamps || {};
  payload.timestamps.client_caption_received_at = Date.now();
  totalLatency.textContent = `${serverLatency} ms / client ${clientReceive} ms`;
  lagMeter.value = serverLatency;
  if (captionSourceBadge) {
    captionSourceBadge.textContent = `audio_source: ${payload.audio_source || "mock"} / ${payload.provider.stt} / ${payload.provider.translator}`;
  }
  if (captionLatencyBreakdown) {
    const latency = payload.latency_ms || {};
    const ingest = latency.ingest_latency_ms ?? 0;
    const stt = latency.first_partial_latency_ms ?? latency.final_latency_ms ?? latency.stt_latency_ms ?? latency.stt ?? 0;
    const translation = latency.translation_latency_ms ?? latency.translation ?? 0;
    const total = latency.total_latency_ms ?? latency.estimated_end_to_end_latency_ms ?? latency.total_server_latency_ms ?? serverLatency;
    captionLatencyBreakdown.textContent = `ingest ${ingest} ms / stt ${stt} ms / translation ${translation} ms / total ${total} ms / client ${clientReceive} ms`;
  }
  if (glossaryDebugBadge) {
    const glossary = payload.glossary || {};
    const normalization = glossary.normalization_changes?.length || 0;
    const postprocess = glossary.postprocess_changes?.length || 0;
    glossaryDebugBadge.textContent = glossary.enabled ? `${normalization} normalized / ${postprocess} post` : "off";
  }
}

if (lessonId) {
  const captionsToken = captionPageToken("caption_token", "student_token", "token");
  window.CaptionSocketState = window.CaptionSocketState || {};
  const existingSocket = window.CaptionSocketState.socket;
  if (existingSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(existingSocket.readyState)) {
    window.CaptionSocketState.duplicate_connect_skipped = (window.CaptionSocketState.duplicate_connect_skipped || 0) + 1;
  } else {
    if (existingSocket && existingSocket.readyState !== WebSocket.CLOSED) existingSocket.close();
    const socket = new WebSocket(captionWebSocketUrl(`/ws/lessons/${lessonId}/captions`, captionsToken));
    window.CaptionSocketState.socket = socket;
    window.CaptionDebug.captions_ws_connections_created += 1;
    socket.onopen = () => {
      statusBadge.textContent = "connected";
      statusBadge.classList.add("running");
    };
    socket.onclose = (event) => {
      statusBadge.textContent = event.code === 4401 || event.code === 4403 ? "auth required" : "disconnected";
      statusBadge.classList.remove("running");
      if (window.CaptionSocketState.socket === socket) window.CaptionSocketState.socket = null;
    };
    socket.onmessage = (event) => {
      window.CaptionDebug.captions_events_received += 1;
      const payload = JSON.parse(event.data);
      updateLatency(payload);
      latestCaptionPayload = payload;
      const text = renderText(payload);
      if (payload.is_partial) {
        latestPartialPayload = payload;
        partialCaption.textContent = text;
        overlay.textContent = text;
        if (captionBelowText) captionBelowText.textContent = text;
        return;
      }
      latestPartialPayload = null;
      overlay.textContent = text;
      if (captionBelowText) captionBelowText.textContent = text;
      partialCaption.textContent = "";
      if (addFinalCaption(payload)) {
        window.StudentTTS?.onFinalCaptionForTts(payload);
      }
    };
  }

  const diagnosticsToken = captionPageToken("diagnostics_token", "teacher_token", "token");
  const debugSocket = new WebSocket(captionWebSocketUrl(`/ws/lessons/${lessonId}/debug`, diagnosticsToken));
  debugSocket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.event === "rtms_status" && rtmsDebugBadge) {
      rtmsDebugBadge.textContent = payload.status || payload.message;
    }
  };
}

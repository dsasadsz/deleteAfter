const ttsPanel = document.querySelector(".student-tts-panel");

function ttsPageToken(...names) {
  const params = new URLSearchParams(window.location.search);
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return "";
}

if (ttsPanel) {
  const TTS_DEDUPE_TTL_MS = 8000;
  const TTS_DEDUPE_MAX_ITEMS = 200;
  const recentlyPlayedTtsKeys = new Map();
  const ttsNodes = {
    status: document.querySelector("#ttsStatus"),
    enabled: document.querySelector("#ttsEnabled"),
    autoplay: document.querySelector("#ttsAutoplay"),
    language: document.querySelector("#ttsLanguage"),
    provider: document.querySelector("#ttsProvider"),
    voice: document.querySelector("#ttsVoice"),
    voiceStatus: document.querySelector("#ttsVoiceStatus"),
    queueMode: document.querySelector("#ttsQueueMode"),
    volume: document.querySelector("#ttsVolume"),
    duckingEnabled: document.querySelector("#ttsDuckingEnabled"),
    duckingStatus: document.querySelector("#ttsDuckingStatus"),
    duckingFallback: document.querySelector("#ttsDuckingFallback"),
    playLatest: document.querySelector("#ttsPlayLatest"),
    stop: document.querySelector("#ttsStop"),
    queued: document.querySelector("#ttsQueued"),
    requests: document.querySelector("#ttsRequests"),
    errors: document.querySelector("#ttsErrors"),
    cacheHits: document.querySelector("#ttsCacheHits"),
    latency: document.querySelector("#ttsLatency"),
    lastError: document.querySelector("#ttsLastError"),
  };

  const ttsState = {
    lessonId: ttsPanel.dataset.lessonId,
    enabled: Boolean(ttsNodes.enabled?.checked),
    autoplay: ttsPanel.dataset.autoplayDefault === "true",
    queueMode: ttsPanel.dataset.queueMode || "sequential",
    queue: [],
    playing: false,
    duckingEnabled: ttsPanel.dataset.duckingEnabled === "true",
    duckingLevel: Number(ttsPanel.dataset.duckingLevel || 0.2),
    duckingRestoreDelayMs: Number(ttsPanel.dataset.duckingRestoreDelayMs || 300),
    duckingActive: false,
    requestController: null,
    playbackGeneration: 0,
    currentObjectUrl: null,
    audio: new Audio(),
    latestPayload: null,
    currentTtsKey: null,
    ttsEnabledAtMs: null,
    ttsLiveBacklogMs: 5000,
    lastTtsQueueClearAtMs: null,
    statusPayload: null,
    voiceAvailable: false,
    audioUrlEnabled: false,
    metrics: {
      requests: 0,
      errors: 0,
      cacheHits: 0,
      latency: 0,
      lastError: "none",
    },
  };

  const ttsToken = ttsPageToken("tts_token", "caption_token", "student_token", "token");
  if (ttsState.enabled) ttsState.ttsEnabledAtMs = Date.now();
  ttsState.audio.volume = Number(ttsPanel.dataset.volumeDefault || 1);
  if (ttsNodes.autoplay) ttsNodes.autoplay.checked = ttsState.autoplay;
  if (ttsNodes.queueMode) ttsNodes.queueMode.value = ttsState.queueMode;
  if (ttsNodes.volume) ttsNodes.volume.value = String(ttsState.audio.volume);
  if (ttsNodes.duckingEnabled) ttsNodes.duckingEnabled.checked = ttsState.duckingEnabled;
  const azureTtsMissingConfigHint = "AZURE_TTS_DEFAULT_VOICE_KK, AZURE_TTS_DEFAULT_VOICE_UZ, AZURE_TTS_DEFAULT_VOICE_ZH, AZURE_TTS_DEFAULT_VOICE_RU";

  function updateTtsMetrics() {
    if (ttsNodes.queued) ttsNodes.queued.textContent = String(ttsState.queue.length);
    if (ttsNodes.requests) ttsNodes.requests.textContent = String(ttsState.metrics.requests);
    if (ttsNodes.errors) ttsNodes.errors.textContent = String(ttsState.metrics.errors);
    if (ttsNodes.cacheHits) ttsNodes.cacheHits.textContent = String(ttsState.metrics.cacheHits);
    if (ttsNodes.latency) ttsNodes.latency.textContent = `${ttsState.metrics.latency} ms`;
    if (ttsNodes.lastError) ttsNodes.lastError.textContent = ttsState.metrics.lastError;
  }

  function setTtsStatus(value) {
    if (ttsNodes.status) ttsNodes.status.textContent = value;
  }

  function setVoiceStatus(value) {
    if (ttsNodes.voiceStatus) ttsNodes.voiceStatus.textContent = value;
  }

  function normalizeTtsText(text) {
    return String(text || "").trim().replace(/\s+/g, " ").toLowerCase();
  }

  function isUnplayableTtsText(text) {
    const normalized = normalizeTtsText(text);
    return (
      !normalized ||
      normalized.startsWith("translation unavailable") ||
      normalized.startsWith("waiting for ") ||
      normalized.startsWith("перевод временно недоступен") ||
      normalized.startsWith("перевод пока недоступен")
    );
  }

  function timestampToMs(value) {
    if (value == null || value === "") return null;
    if (typeof value === "number" && Number.isFinite(value)) {
      return value < 1000000000000 ? value * 1000 : value;
    }
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function captionTimestampMs(payload, receiveTimeMs = Date.now()) {
    return (
      timestampToMs(payload?.timestamps?.websocket_sent_at) ||
      timestampToMs(payload?.timestamps?.translation_done_at) ||
      timestampToMs(payload?.created_at) ||
      timestampToMs(payload?.timestamps?.client_caption_received_at) ||
      receiveTimeMs
    );
  }

  function isFreshTtsCaption(payload, receiveTimeMs = Date.now()) {
    const enabledAtMs = ttsState.ttsEnabledAtMs || receiveTimeMs;
    const captionTimeMs = captionTimestampMs(payload, receiveTimeMs);
    return captionTimeMs >= enabledAtMs - ttsState.ttsLiveBacklogMs;
  }

  function ttsKeyForPayload(payload, rendered) {
    const language = rendered?.language || effectiveTtsLanguage();
    if (payload?.caption_id) return `caption_id:${payload.caption_id}:${language}`;
    if (payload?.text_hash) return `text_hash:${payload.text_hash}:${language}`;
    return `text:${normalizeTtsText(rendered?.text)}:${language}`;
  }

  function pruneRecentlyPlayedTtsKeys(now = Date.now()) {
    for (const [key, seenAt] of recentlyPlayedTtsKeys.entries()) {
      if (now - seenAt > TTS_DEDUPE_TTL_MS) recentlyPlayedTtsKeys.delete(key);
    }
    while (recentlyPlayedTtsKeys.size > TTS_DEDUPE_MAX_ITEMS) {
      const oldestKey = recentlyPlayedTtsKeys.keys().next().value;
      recentlyPlayedTtsKeys.delete(oldestKey);
    }
  }

  function rememberTtsKey(key, now = Date.now()) {
    recentlyPlayedTtsKeys.set(key, now);
    pruneRecentlyPlayedTtsKeys(now);
  }

  function isDuplicateTtsKey(key, now = Date.now()) {
    pruneRecentlyPlayedTtsKeys(now);
    return recentlyPlayedTtsKeys.has(key);
  }

  async function refreshTtsStatus() {
    try {
      const response = await fetch("/api/tts/status");
      if (!response.ok) throw new Error(`status ${response.status}`);
      const payload = await response.json();
      ttsState.statusPayload = payload;
      ttsState.audioUrlEnabled = Boolean(payload.audio_url_enabled);
      const ready = payload.enabled && payload.ready;
      const missing = Array.isArray(payload.missing) ? payload.missing : [];
      const missingText = missing.length ? ` missing: ${missing.join(", ")}` : "";
      setTtsStatus(ready ? `ready (${payload.provider})` : `unavailable (${payload.provider || "unknown"}${missingText})`);
      if (ttsNodes.enabled) ttsNodes.enabled.disabled = !ready;
      updateProviderOptions(payload);
      updateVoiceOptions();
      if (!ready) {
        ttsState.enabled = false;
        ttsState.metrics.lastError = missing.length ? `Missing TTS config: ${missing.join(", ")}` : `TTS unavailable${payload.provider === "azure" ? `; check ${azureTtsMissingConfigHint}` : ""}`;
        updateTtsMetrics();
      }
    } catch (error) {
      setTtsStatus("unavailable");
      ttsState.metrics.lastError = error.message || "status unavailable";
      updateTtsMetrics();
    }
  }

  function updateProviderOptions(payload) {
    if (!ttsNodes.provider) return;
    const providers = payload.providers || payload.selected_voice_support?.providers || {};
    const currentProvider = payload.active_provider || payload.provider || "azure";
    for (const option of Array.from(ttsNodes.provider.options || [])) {
      const providerStatus = providers[option.value];
      option.disabled = false;
      if (option.value === "azure") option.textContent = providerStatus?.status ? `Azure (${providerStatus.status})` : "Azure";
      if (option.value === "elevenlabs" && providerStatus?.status) {
        option.textContent = `ElevenLabs experimental (${providerStatus.status})`;
      }
    }
    if (!ttsNodes.provider.value) ttsNodes.provider.value = currentProvider;
  }

  function effectiveTtsLanguage() {
    const selected = ttsNodes.language?.value || "follow";
    if (selected !== "follow") return selected;
    return window.CaptionState?.selectedLanguage?.() || "all";
  }

  function selectedTtsProvider() {
    const selected = ttsNodes.provider?.value || "";
    if (!selected || selected === "auto") return ttsState.statusPayload?.active_provider || ttsState.statusPayload?.provider || "";
    return selected;
  }

  function selectedVoice() {
    return ttsNodes.voice?.value || "";
  }

  function languageLabel(language) {
    return {kk: "Kazakh", uz: "Uzbek", "zh-Hans": "Chinese", ru: "Russian"}[language] || language;
  }

  function providerLabel(providerName) {
    if (providerName === "elevenlabs") return "ElevenLabs";
    return providerName ? providerName[0].toUpperCase() + providerName.slice(1) : "TTS";
  }

  function voiceLanguage() {
    const language = effectiveTtsLanguage();
    return language === "original" ? "ru" : language;
  }

  function updateVoiceOptions() {
    const language = voiceLanguage();
    if (!ttsNodes.voice) return;
    const providerName = selectedTtsProvider() || ttsState.statusPayload?.active_provider || ttsState.statusPayload?.provider || "";
    const providerStatus = ttsState.statusPayload?.providers?.[providerName];
    const providerReady = providerStatus ? Boolean(providerStatus.ready) : Boolean(ttsState.statusPayload?.ready);
    if (ttsNodes.enabled) ttsNodes.enabled.disabled = !(ttsState.statusPayload?.enabled && providerReady);
    if (language === "all") {
      replaceVoiceOptions([]);
      ttsNodes.voice.disabled = true;
      ttsState.voiceAvailable = false;
      if (ttsNodes.playLatest) ttsNodes.playLatest.disabled = true;
      setVoiceStatus("Choose explicit TTS language/voice for autoplay");
      return;
    }
    const allVoices = providerStatus?.voices?.[language] || ttsState.statusPayload?.voices?.[language] || [];
    if (!providerStatus && !ttsState.statusPayload?.voices) {
      ttsState.voiceAvailable = true;
      ttsNodes.voice.disabled = false;
      if (ttsNodes.playLatest) ttsNodes.playLatest.disabled = false;
      setVoiceStatus("Voice catalog unavailable; using provider default");
      return;
    }
    const voices = allVoices;
    const previousVoice = ttsNodes.voice.value || "";
    replaceVoiceOptions(voices);
    const availableIds = voices.map((voice) => voice.id);
    if (previousVoice && availableIds.includes(previousVoice)) {
      ttsNodes.voice.value = previousVoice;
    } else {
      const defaultVoice = providerStatus?.default_voice_by_language?.[language] || ttsState.statusPayload?.default_voice_by_language?.[language] || "";
      ttsNodes.voice.value = availableIds.includes(defaultVoice) ? defaultVoice : (availableIds[0] || "");
    }
    const selected = voices.find((voice) => voice.id === ttsNodes.voice.value);
    ttsNodes.voice.disabled = !voices.length;
    ttsState.voiceAvailable = Boolean(selected);
    if (ttsNodes.playLatest) ttsNodes.playLatest.disabled = !ttsState.voiceAvailable;
    const selectedProviderLabel = providerLabel(providerName);
    const selectedLanguageLabel = languageLabel(language);
    if (selected) {
      setVoiceStatus(`Using ${selected.id}`);
    } else if (voices.length) {
      setVoiceStatus(`${voices.length} ${selectedProviderLabel} voices available for ${selectedLanguageLabel}`);
    } else {
      setVoiceStatus(`No ${selectedProviderLabel} voices configured for ${selectedLanguageLabel}`);
    }
    if (voices.length && !selected) setVoiceStatus(`${voices.length} ${selectedProviderLabel} voices available for ${selectedLanguageLabel}`);
  }

  function replaceVoiceOptions(voices) {
    if (!ttsNodes.voice) return;
    ttsNodes.voice.innerHTML = "";
    if (Array.isArray(ttsNodes.voice.options)) ttsNodes.voice.options.length = 0;
    for (const voice of voices) {
      const option = document.createElement("option");
      option.value = voice.id;
      option.textContent = voice.display_name || voice.name || voice.id;
      if (voice.experimental) option.textContent += " experimental";
      ttsNodes.voice.appendChild(option);
    }
  }

  function canSynthesizeTts() {
    if (!ttsState.statusPayload) return true;
    const language = voiceLanguage();
    const providerName = selectedTtsProvider();
    const providerStatus = ttsState.statusPayload?.providers?.[providerName];
    const providerReady = providerStatus ? Boolean(providerStatus.ready) : Boolean(ttsState.statusPayload?.ready);
    return language !== "all" && providerReady && ttsState.voiceAvailable;
  }

  function revokeCurrentObjectUrl() {
    if (ttsState.currentObjectUrl) {
      URL.revokeObjectURL(ttsState.currentObjectUrl);
      ttsState.currentObjectUrl = null;
    }
  }

  function stopTtsPlayback() {
    ttsState.playbackGeneration += 1;
    if (ttsState.requestController) {
      ttsState.requestController.abort();
      ttsState.requestController = null;
    }
    revokeCurrentObjectUrl();
    ttsState.queue = [];
    ttsState.playing = false;
    ttsState.currentTtsKey = null;
    ttsState.audio.pause();
    ttsState.audio.removeAttribute("src");
    ttsState.audio.load();
    endTtsDucking();
    updateTtsMetrics();
  }

  function clearTtsQueueAndPlayback() {
    ttsState.lastTtsQueueClearAtMs = Date.now();
    stopTtsPlayback();
  }

  const manualDuckingInstruction = "Lower Zoom volume manually or mute original audio while TTS is playing.";

  function setDuckingStatus(status, showFallback) {
    if (ttsNodes.duckingStatus) ttsNodes.duckingStatus.textContent = status;
    if (ttsNodes.duckingFallback) {
      ttsNodes.duckingFallback.textContent = manualDuckingInstruction;
      ttsNodes.duckingFallback.hidden = !showFallback;
    }
  }

  function beginTtsDucking() {
    if (!ttsState.duckingEnabled) return;
    const result = window.ZoomAudioDucking?.duck?.(ttsState.duckingLevel);
    const controllable = Boolean(result?.controllable);
    ttsState.duckingActive = controllable;
    setDuckingStatus(controllable ? "ducked" : "unavailable", !controllable);
  }

  function endTtsDucking(force = false) {
    if (!force && !ttsState.duckingEnabled) return;
    window.ZoomAudioDucking?.restore?.(ttsState.duckingRestoreDelayMs);
    setDuckingStatus(ttsState.duckingActive ? "restored" : "unavailable", false);
    ttsState.duckingActive = false;
  }

  window.addEventListener("zoom-audio-ducking-status", (event) => {
    if (!ttsState.duckingActive) setDuckingStatus(event.detail?.status || "unavailable", false);
  });

  function enqueueTts(payload) {
    if (!payload || payload.is_partial) return;
    const receiveTimeMs = Date.now();
    if (!isFreshTtsCaption(payload, receiveTimeMs)) {
      setTtsStatus("Skipped old caption");
      return;
    }
    const renderTts = window.CaptionRendering?.ttsTextForLanguage;
    if (!renderTts) return;
    if (!canSynthesizeTts()) {
      setTtsStatus("TTS skipped: no voice available");
      return;
    }
    const rendered = renderTts(payload, effectiveTtsLanguage());
    if (!rendered || isUnplayableTtsText(rendered.text)) {
      setTtsStatus("TTS skipped: translation unavailable");
      return;
    }
    const key = ttsKeyForPayload(payload, rendered);
    if (isDuplicateTtsKey(key)) {
      setTtsStatus("TTS skipped duplicate final");
      return;
    }
    if (ttsState.queueMode === "latest_only") stopTtsPlayback();
    if (ttsState.currentTtsKey === key || ttsState.queue.some((item) => item.key === key)) {
      setTtsStatus("TTS skipped duplicate final");
      return;
    }
    rememberTtsKey(key);
    ttsState.queue.push({
      ...rendered,
      key,
      captionId: payload.caption_id,
      sequence: payload.sequence,
      provider: selectedTtsProvider(),
      voice: selectedVoice(),
    });
    updateTtsMetrics();
    playNextTts();
  }

  async function playNextTts() {
    if (ttsState.playing || !ttsState.queue.length) return;
    const item = ttsState.queue.shift();
    const requestController = new AbortController();
    const playbackGeneration = ttsState.playbackGeneration;
    let objectUrl = null;
    ttsState.requestController = requestController;
    ttsState.playing = true;
    ttsState.currentTtsKey = item.key;
    ttsState.metrics.requests += 1;
    updateTtsMetrics();
    try {
      const url = new URL(`/api/lessons/${ttsState.lessonId}/tts/synthesize`, window.location.origin);
      if (ttsToken) url.searchParams.set("token", ttsToken);
      const requestBody = {
        text: item.text,
        language: item.language,
        provider: item.provider || undefined,
        voice: item.voice || undefined,
        caption_id: item.captionId || undefined,
        sequence: item.sequence,
        ...(ttsState.audioUrlEnabled ? { return_mode: "url" } : {}),
      };
      let response = await fetch(url.toString(), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        signal: requestController.signal,
        body: JSON.stringify(requestBody),
      });
      if (!response.ok && ttsState.audioUrlEnabled) {
        setTtsStatus("audio url failed, retrying direct audio");
        const directBody = {...requestBody};
        delete directBody.return_mode;
        response = await fetch(url.toString(), {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          signal: requestController.signal,
          body: JSON.stringify(directBody),
        });
      }
      if (playbackGeneration !== ttsState.playbackGeneration) return;
      if (!response.ok) throw new Error(await ttsErrorMessage(response));
      const contentType = response.headers.get("content-type") || "";
      if (ttsState.audioUrlEnabled && contentType.includes("application/json")) {
        const audioPayload = await response.json();
        if (playbackGeneration !== ttsState.playbackGeneration) return;
        setTtsStatus("audio url received");
        if (audioPayload.cached) ttsState.metrics.cacheHits += 1;
        ttsState.metrics.latency = 0;
        revokeCurrentObjectUrl();
        ttsState.audio.src = new URL(audioPayload.audio_url, window.location.origin).toString();
      } else {
        setTtsStatus("audio response received");
        if (response.headers.get("x-tts-cached") === "true") ttsState.metrics.cacheHits += 1;
        ttsState.metrics.latency = Number(response.headers.get("x-tts-latency-ms") || 0);
        const blob = await response.blob();
        if (playbackGeneration !== ttsState.playbackGeneration) return;
        objectUrl = URL.createObjectURL(blob);
        if (playbackGeneration !== ttsState.playbackGeneration) {
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          return;
        }
        revokeCurrentObjectUrl();
        ttsState.currentObjectUrl = objectUrl;
        ttsState.audio.src = objectUrl;
      }
      ttsState.audio.volume = Number(ttsNodes.volume?.value || ttsState.audio.volume || 1);
      ttsState.audio.onended = () => {
        if (playbackGeneration !== ttsState.playbackGeneration) return;
        setTtsStatus("play ended");
        endTtsDucking();
        revokeCurrentObjectUrl();
        if (ttsState.requestController === requestController) ttsState.requestController = null;
        ttsState.playing = false;
        ttsState.currentTtsKey = null;
        updateTtsMetrics();
        playNextTts();
      };
      ttsState.audio.onerror = () => {
        if (playbackGeneration !== ttsState.playbackGeneration) return;
        setTtsStatus("playback error");
        endTtsDucking();
        revokeCurrentObjectUrl();
        if (ttsState.requestController === requestController) ttsState.requestController = null;
        ttsState.metrics.errors += 1;
        ttsState.metrics.lastError = "audio playback failed";
        ttsState.playing = false;
        ttsState.currentTtsKey = null;
        updateTtsMetrics();
        playNextTts();
      };
      beginTtsDucking();
      await ttsState.audio.play();
      setTtsStatus("play started");
    } catch (error) {
      if (playbackGeneration !== ttsState.playbackGeneration) {
        if (objectUrl && objectUrl !== ttsState.currentObjectUrl) URL.revokeObjectURL(objectUrl);
        return;
      }
      if (objectUrl === ttsState.currentObjectUrl) {
        revokeCurrentObjectUrl();
      } else if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
      endTtsDucking();
      if (error.name === "AbortError") return;
      ttsState.metrics.errors += 1;
      const blocked = error.name === "NotAllowedError" || error.name === "NotSupportedError";
      setTtsStatus(blocked ? "play blocked by browser" : "playback error");
      ttsState.metrics.lastError = blocked ? `${error.name}: ${error.message || "browser blocked playback"}` : error.message || "TTS failed";
      ttsState.playing = false;
      ttsState.currentTtsKey = null;
      updateTtsMetrics();
      playNextTts();
    } finally {
      if (ttsState.requestController === requestController) ttsState.requestController = null;
    }
  }

  async function ttsErrorMessage(response) {
    if (response.status === 429) return "Too many requests, please wait.";
    try {
      const payload = await response.json();
      return payload.detail?.message || payload.error?.message || `synthesize ${response.status}`;
    } catch (_) {
      return `synthesize ${response.status}`;
    }
  }

  function onFinalCaptionForTts(payload) {
    ttsState.latestPayload = payload;
    if (!ttsState.enabled || !ttsState.autoplay || payload.is_partial) return;
    enqueueTts(payload);
  }

  ttsNodes.enabled?.addEventListener("change", (event) => {
    ttsState.enabled = event.target.checked;
    if (ttsState.enabled) {
      ttsState.ttsEnabledAtMs = Date.now();
      clearTtsQueueAndPlayback();
      setTtsStatus("TTS enabled for live captions only");
    } else {
      ttsState.ttsEnabledAtMs = null;
      clearTtsQueueAndPlayback();
      setTtsStatus("TTS disabled");
    }
  });
  ttsNodes.autoplay?.addEventListener("change", (event) => {
    ttsState.autoplay = event.target.checked;
  });
  ttsNodes.queueMode?.addEventListener("change", (event) => {
    ttsState.queueMode = event.target.value;
  });
  ttsNodes.language?.addEventListener("change", updateVoiceOptions);
  ttsNodes.provider?.addEventListener("change", updateVoiceOptions);
  ttsNodes.voice?.addEventListener("change", updateVoiceOptions);
  document.querySelector("#languageSelector")?.addEventListener("change", updateVoiceOptions);
  ttsNodes.volume?.addEventListener("input", (event) => {
    ttsState.audio.volume = Number(event.target.value);
  });
  ttsNodes.duckingEnabled?.addEventListener("change", (event) => {
    const duckingEnabled = event.target.checked;
    if (!duckingEnabled) endTtsDucking(true);
    ttsState.duckingEnabled = duckingEnabled;
  });
  ttsNodes.playLatest?.addEventListener("click", () => {
    if (ttsState.latestPayload) enqueueTts(ttsState.latestPayload);
  });
  ttsNodes.stop?.addEventListener("click", stopTtsPlayback);

  window.StudentTTS = {onFinalCaptionForTts};
  updateTtsMetrics();
  refreshTtsStatus();
} else {
  window.StudentTTS = {
    onFinalCaptionForTts() {},
  };
}

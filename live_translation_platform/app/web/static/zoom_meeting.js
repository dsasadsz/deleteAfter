const studentRoot = document.querySelector(".student-layout");
const zoomJoinButton = document.querySelector("#joinZoomVideo");
const zoomStatus = document.querySelector("#zoomVideoStatus");
const videoMessage = document.querySelector("#videoMessage");
const meetingElement = document.querySelector("#meetingSDKElement");
const videoCenter = document.querySelector(".video-center");
let zoomJoinInFlight = false;
const loggedZoomEmbedErrors = new Set();

function createZoomAudioDucking(root) {
  const state = {
    client: null,
    status: "unavailable",
    originals: new Map(),
    restoreTimer: null,
  };

  function mediaElements() {
    return Array.from(root?.querySelectorAll("audio, video") || [])
      .filter((item) => typeof item.volume === "number");
  }

  function setStatus(value) {
    state.status = value;
    window.dispatchEvent(new CustomEvent("zoom-audio-ducking-status", { detail: { status: value } }));
  }

  function setClient(client) {
    state.client = client;
    setStatus(mediaElements().length > 0 ? "controllable" : "unavailable");
  }

  function duck(level) {
    const elements = mediaElements();
    if (elements.length === 0) {
      setStatus("unavailable");
      return { controllable: false, status: state.status };
    }

    if (state.restoreTimer) clearTimeout(state.restoreTimer);
    state.restoreTimer = null;
    const requestedLevel = Number(level);
    const volume = Number.isFinite(requestedLevel) ? Math.min(1, Math.max(0, requestedLevel)) : 1;
    elements.forEach((element) => {
      if (!state.originals.has(element)) state.originals.set(element, element.volume);
      element.volume = volume;
    });
    setStatus("ducked");
    return { controllable: true, status: state.status };
  }

  function restore(delayMs) {
    if (state.restoreTimer) clearTimeout(state.restoreTimer);
    const delay = Math.max(0, Number(delayMs) || 0);
    state.restoreTimer = setTimeout(() => {
      const elements = mediaElements();
      state.originals.forEach((volume, element) => {
        element.volume = volume;
      });
      state.originals.clear();
      state.restoreTimer = null;
      setStatus(elements.length > 0 ? "restored" : "unavailable");
    }, delay);
  }

  return { status: () => state.status, setClient, duck, restore };
}

window.ZoomAudioDucking = createZoomAudioDucking(meetingElement);

function setZoomStatus(status, message) {
  if (zoomStatus) zoomStatus.textContent = status;
  if (videoMessage && message) videoMessage.textContent = message;
}

function zoomEmbedConfigUrl(lessonId) {
  const token = new URLSearchParams(window.location.search).get("embed_token")
    || new URLSearchParams(window.location.search).get("student_token")
    || new URLSearchParams(window.location.search).get("token");
  if (!token) return `/api/lessons/${lessonId}/zoom/embed-config`;
  const url = new URL(`/api/v1/integration/lessons/${lessonId}/zoom/embed-config`, window.location.origin);
  url.searchParams.set("token", token);
  return url.toString();
}

async function joinZoomVideo() {
  const lessonId = studentRoot?.dataset.lessonId;
  if (!lessonId) return;
  if (zoomJoinInFlight) return;
  zoomJoinInFlight = true;
  if (zoomJoinButton) zoomJoinButton.disabled = true;
  setZoomStatus("loading config", "Fetching Meeting SDK signature from backend.");
  let response;
  let config;
  try {
    response = await fetch(zoomEmbedConfigUrl(lessonId));
    config = await response.json();
  } catch (error) {
    setZoomStatus("error", "Could not load Zoom Meeting SDK configuration.");
    logZoomEmbedError("ZOOM_CONFIG_FETCH_FAILED", error?.message || "Fetch failed");
    zoomJoinInFlight = false;
    if (zoomJoinButton) zoomJoinButton.disabled = false;
    return;
  }
  if (!response.ok) {
    logZoomEmbedError(zoomEmbedErrorCode(config), zoomEmbedErrorRawMessage(config));
    setZoomStatus("not configured", zoomEmbedErrorMessage(response, config));
    zoomJoinInFlight = false;
    if (zoomJoinButton) zoomJoinButton.disabled = false;
    return;
  }
  if (config.mode === "mock") {
    setZoomStatus("mock", config.message || "Mock lesson uses local placeholder.");
    zoomJoinInFlight = false;
    if (zoomJoinButton) zoomJoinButton.disabled = false;
    return;
  }
  if (!window.ZoomMtgEmbedded) {
    setZoomStatus("error", "Zoom Meeting SDK script did not load.");
    zoomJoinInFlight = false;
    if (zoomJoinButton) zoomJoinButton.disabled = false;
    return;
  }
  try {
    setZoomStatus("joining", "Joining embedded Zoom meeting.");
    meetingElement.hidden = false;
    videoCenter.hidden = true;
    const client = window.ZoomMtgEmbedded.createClient();
    client.init({
      zoomAppRoot: meetingElement,
      language: config.lang || "en-US",
      customize: {
        video: { isResizable: true, viewSizes: { default: { width: 900, height: 520 } } },
      },
    });
    await client.join({
      sdkKey: config.sdk_key_or_client_id,
      signature: config.signature,
      meetingNumber: config.meeting_number,
      password: config.password || "",
      userName: config.user_name || "Student",
    });
    window.ZoomAudioDucking?.setClient?.(client);
    setZoomStatus("joined", "Embedded Zoom meeting joined. Captions are still delivered by Python WebSocket.");
  } catch (error) {
    meetingElement.hidden = true;
    videoCenter.hidden = false;
    setZoomStatus("error", error?.reason || error?.message || "Could not join Zoom meeting.");
    zoomJoinInFlight = false;
    if (zoomJoinButton) zoomJoinButton.disabled = false;
  }
}

zoomJoinButton?.addEventListener("click", joinZoomVideo);

function zoomEmbedErrorMessage(response, payload) {
  if (response.status === 401 || response.status === 403) return "Zoom embed token is missing or not allowed.";
  const detail = payload?.detail;
  const code = typeof detail === "object" ? detail.code : "";
  if (code === "ZOOM_SDK_NOT_CONFIGURED") return "Zoom Meeting SDK credentials are not configured.";
  if (code === "LESSON_ZOOM_NOT_READY") return "Lesson does not have Zoom meeting metadata yet.";
  if (code === "ZOOM_NOT_AVAILABLE_FOR_MOCK_LESSON") return "Zoom video is not available for this mock lesson.";
  if (code === "ZOOM_SIGNATURE_FAILED") return "Zoom Meeting SDK signature generation failed.";
  if (code === "LESSON_NOT_FOUND") return "Lesson not found.";
  if (typeof detail === "object" && detail.message) return detail.message;
  if (typeof detail === "string") return detail;
  return "Zoom Meeting SDK configuration is not ready.";
}

function zoomEmbedErrorCode(payload) {
  const detail = payload?.detail;
  return typeof detail === "object" && detail.code ? detail.code : "ZOOM_EMBED_CONFIG_FAILED";
}

function zoomEmbedErrorRawMessage(payload) {
  const detail = payload?.detail;
  if (typeof detail === "object" && detail.message) return detail.message;
  if (typeof detail === "string") return detail;
  return "Zoom embed config failed";
}

function logZoomEmbedError(code, message) {
  const key = `${code}:${message}`;
  if (loggedZoomEmbedErrors.has(key)) return;
  loggedZoomEmbedErrors.add(key);
  console.info("zoom_embed_config_error", { code, message });
}

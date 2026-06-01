const wsUrlInput = document.getElementById("wsUrl");
const tokenInput = document.getElementById("accessToken");
const languageSelect = document.getElementById("language");
const captionBox = document.getElementById("caption");
const logBox = document.getElementById("log");
const connectButton = document.getElementById("connect");

let socket;

connectButton.addEventListener("click", () => {
  if (socket) {
    socket.close();
  }
  const url = new URL(wsUrlInput.value);
  if (tokenInput.value) {
    url.searchParams.set("token", tokenInput.value);
  }
  socket = new WebSocket(url);
  socket.onopen = () => writeLog("connected");
  socket.onclose = () => writeLog("closed");
  socket.onerror = () => writeLog("websocket error");
  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.event !== "caption") {
      writeLog(JSON.stringify(event, null, 2));
      return;
    }
    renderCaption(event);
  };
});

function renderCaption(event) {
  const language = languageSelect.value;
  if (language === "all") {
    captionBox.innerHTML = `
      <strong>RU:</strong> ${escapeHtml(event.original_text_normalized || event.original_text_raw || "")}<br>
      <strong>KK:</strong> ${escapeHtml(event.translations?.kk || "")}<br>
      <strong>UZ:</strong> ${escapeHtml(event.translations?.uz || "")}<br>
      <strong>ZH:</strong> ${escapeHtml(event.translations?.["zh-Hans"] || "")}
    `;
    return;
  }
  const text = language === "original"
    ? event.original_text_normalized || event.original_text_raw || ""
    : event.translations?.[language] || "";
  captionBox.textContent = text;
}

function writeLog(text) {
  logBox.textContent = `${new Date().toISOString()} ${text}\n${logBox.textContent}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

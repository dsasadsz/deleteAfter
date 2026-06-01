const languageSelect = document.querySelector("#transcriptLanguage");
const searchInput = document.querySelector("#transcriptSearch");
const notesResult = document.querySelector("#notesResult");
const toast = document.querySelector("#toast");

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  setTimeout(() => {
    toast.hidden = true;
  }, 4500);
}

function renderSegments() {
  const language = languageSelect?.value || "all";
  const query = (searchInput?.value || "").toLowerCase();
  document.querySelectorAll(".transcript-segment").forEach((segment) => {
    const textNode = segment.querySelector(".segmentText");
    const allText = [
      segment.dataset.raw,
      segment.dataset.ru,
      segment.dataset.kk,
      segment.dataset.uz,
      segment.dataset.zhHans,
    ].join("\n");
    if (language === "all") {
      textNode.textContent = `RU raw: ${segment.dataset.raw}
RU normalized: ${segment.dataset.ru}
kk: ${segment.dataset.kk || "Translation unavailable"}
uz: ${segment.dataset.uz || "Translation unavailable"}
zh-Hans: ${segment.dataset.zhHans || "Translation unavailable"}`;
    } else if (language === "raw") {
      textNode.textContent = segment.dataset.raw || "";
    } else if (language === "ru") {
      textNode.textContent = segment.dataset.ru || "";
    } else {
      textNode.textContent = segment.dataset[language === "zh-Hans" ? "zhHans" : language] || "Translation unavailable";
    }
    segment.hidden = query && !allText.toLowerCase().includes(query);
  });
}

languageSelect?.addEventListener("change", renderSegments);
searchInput?.addEventListener("input", renderSegments);

document.querySelector("#generateSimpleNotes")?.addEventListener("click", async (event) => {
  const lessonId = event.currentTarget.dataset.lessonId;
  try {
    const response = await fetch(`/api/lessons/${lessonId}/notes/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: "ru", mode: "simple", include_glossary_terms: true }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Notes generation failed");
    notesResult.hidden = false;
    notesResult.innerHTML = `<strong>Simple notes generated</strong><pre>${escapeHtml(payload.content_markdown)}</pre>`;
  } catch (error) {
    showToast(error.message);
  }
});

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

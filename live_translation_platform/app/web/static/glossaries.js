const glossaryToast = document.querySelector("#toast");

function showGlossaryToast(message) {
  if (!glossaryToast) return;
  glossaryToast.textContent = message;
  glossaryToast.hidden = false;
  setTimeout(() => {
    glossaryToast.hidden = true;
  }, 4500);
}

function languagesFrom(selector) {
  return document.querySelector(selector).value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

document.querySelector("#loadProgrammingGlossary")?.addEventListener("click", async () => {
  try {
    await readJson(await fetch("/api/glossaries/defaults/programming-ru", { method: "POST" }));
    window.location.reload();
  } catch (error) {
    showGlossaryToast(error.message);
  }
});

document.querySelector("#glossaryCreateForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = await readJson(
      await fetch("/api/glossaries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: document.querySelector("#glossaryName").value,
          domain: document.querySelector("#glossaryDomain").value,
          description: document.querySelector("#glossaryDescription").value,
          source_language: "ru-RU",
          target_languages: languagesFrom("#glossaryLanguages"),
        }),
      }),
    );
    window.location.href = `/glossaries/${payload.id}`;
  } catch (error) {
    showGlossaryToast(error.message);
  }
});

document.querySelector("#termCreateForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const glossaryId = event.currentTarget.dataset.glossaryId;
  try {
    let translations = {};
    const rawTranslations = document.querySelector("#termTranslations").value.trim();
    if (rawTranslations) translations = JSON.parse(rawTranslations);
    await readJson(
      await fetch(`/api/glossaries/${glossaryId}/terms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: document.querySelector("#termSource").value,
          canonical: document.querySelector("#termCanonical").value,
          aliases: languagesFrom("#termAliases"),
          translations,
          match_type: document.querySelector("#termMatchType").value,
          priority: Number.parseInt(document.querySelector("#termPriority").value || "0", 10),
          enabled: true,
        }),
      }),
    );
    window.location.reload();
  } catch (error) {
    showGlossaryToast(error.message);
  }
});

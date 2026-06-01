const usageToast = document.querySelector("#toast");

function showUsageToast(message) {
  if (!usageToast) return;
  usageToast.textContent = message;
  usageToast.hidden = false;
  setTimeout(() => {
    usageToast.hidden = true;
  }, 4500);
}

document.querySelector("#loadDefaultPricing")?.addEventListener("click", async () => {
  const response = await fetch("/api/usage/pricing/defaults", { method: "POST" });
  const payload = await response.json();
  if (!response.ok) {
    showUsageToast(payload.detail || "Could not load pricing");
    return;
  }
  window.location.reload();
});

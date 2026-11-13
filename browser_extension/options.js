const ids = ["haUrl", "token", "addon"];
const statusEl = document.getElementById("status");

function show(status) {
  if (!status) return;
  statusEl.textContent = `${status.text} (${new Date(status.at).toLocaleString()})`;
  statusEl.className = status.ok ? "ok" : "bad";
}

chrome.storage.local.get([...ids, "status"]).then((s) => {
  for (const id of ids) document.getElementById(id).value = s[id] || "";
  show(s.status);
});
chrome.storage.onChanged.addListener((c) => c.status && show(c.status.newValue));

document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = Object.fromEntries(ids.map((id) => [id, document.getElementById(id).value.trim()]));
  let origin;
  try {
    origin = new URL(v.haUrl).origin;
  } catch {
    statusEl.textContent = "That Home Assistant URL is not valid.";
    statusEl.className = "bad";
    return;
  }
  // Must be asked for from this click: Chrome only shows the prompt for a user gesture.
  const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
  if (!granted) {
    statusEl.textContent = "The extension needs permission to reach your Home Assistant.";
    statusEl.className = "bad";
    return;
  }
  await chrome.storage.local.set({ ...v, haUrl: origin });
  statusEl.textContent = "Syncing…";
  statusEl.className = "note";
  chrome.runtime.sendMessage("sync");
});

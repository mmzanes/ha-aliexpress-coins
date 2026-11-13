// Sends the AliExpress login to the AliExpress Coins add-on through Home Assistant's
// hassio.addon_stdin action whenever the login ticket (xman_t) changes, plus a
// check twice a day. The add-on loads the cookies into its own Chromium and checks in.

const LOGIN_COOKIES = ["xman_t", "xman_us_t"];
const DEBOUNCE_MS = 15000; // a login sets many cookies in a burst; send once it settles
const PERIOD_MIN = 12 * 60;

async function settings() {
  return chrome.storage.local.get(["haUrl", "token", "addon", "lastHash"]);
}

async function setStatus(text, ok) {
  await chrome.storage.local.set({ status: { text, ok, at: new Date().toISOString() } });
  chrome.action.setBadgeText({ text: ok ? "" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: "#c92a2a" });
}

async function hash(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Same field names as a Cookie-Editor export, which the add-on already reads.
function exportCookie(c) {
  return {
    name: c.name, value: c.value, domain: c.domain, path: c.path,
    expirationDate: c.expirationDate, session: c.session,
    httpOnly: c.httpOnly, secure: c.secure, sameSite: c.sameSite,
  };
}

// force: send even if this login was already sent (the toolbar button, "Sync now").
async function sync(force = false) {
  const s = await settings();
  if (!s.haUrl || !s.token || !s.addon) {
    await setStatus("Not set up: open the extension's options.", false);
    return;
  }
  const cookies = await chrome.cookies.getAll({ domain: "aliexpress.com" });
  const ticket = cookies
    .filter((c) => LOGIN_COOKIES.includes(c.name))
    .map((c) => `${c.name}=${c.value}`)
    .sort()
    .join(";");
  if (!ticket.includes("xman_t=")) {
    await setStatus("Not logged in to AliExpress in this browser.", false);
    return;
  }
  const h = await hash(ticket);
  if (!force && h === s.lastHash) return;
  const url = `${s.haUrl.replace(/\/+$/, "")}/api/services/hassio/addon_stdin`;
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${s.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ addon: s.addon, input: { cookies: cookies.map(exportCookie) } }),
    });
  } catch (e) {
    await setStatus(`Could not reach Home Assistant: ${e.message}`, false);
    return;
  }
  if (!res.ok) {
    const hint = res.status === 401 ? " (check the token)" : res.status === 400 ? " (check the add-on slug)" : "";
    await setStatus(`Home Assistant answered HTTP ${res.status}${hint}.`, false);
    return;
  }
  await chrome.storage.local.set({ lastHash: h });
  await setStatus(`Sent ${cookies.length} cookies. The add-on imports them and checks in.`, true);
}

let timer;
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || !LOGIN_COOKIES.includes(cookie.name) || !cookie.domain.includes("aliexpress")) return;
  clearTimeout(timer);
  timer = setTimeout(() => sync(), DEBOUNCE_MS);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("sync", { periodInMinutes: PERIOD_MIN });
  chrome.runtime.openOptionsPage();
});
chrome.runtime.onStartup.addListener(() => sync());
chrome.alarms.onAlarm.addListener((a) => a.name === "sync" && sync());
chrome.action.onClicked.addListener(() => sync(true));
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg === "sync") sync(true).then(() => reply(true));
  return true;
});

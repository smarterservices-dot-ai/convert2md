import { defaultFilename, normalizeDownloadFilename } from "./md.js";

const $ = (id) => document.getElementById(id);
const PREF_KEY = "popupPrefs";

const RESTRICTED_PREFIXES = [
  "chrome://",
  "chrome-extension://",
  "chrome-search://",
  "chrome-untrusted://",
  "devtools://",
  "view-source:",
  "edge://",
  "about:",
  "https://chromewebstore.google.com/",
  "https://chrome.google.com/webstore",
];

const UNSUPPORTED_HOSTS = [
  { match: /docs\.google\.com\/document/, why: "Google Docs canvas is not supported." },
];

let activeTab = null;

function restrictedReason(url) {
  if (!url) return "No URL for this tab.";
  for (const prefix of RESTRICTED_PREFIXES) {
    if (url.startsWith(prefix)) return "Chrome blocks extensions on this page.";
  }
  for (const rule of UNSUPPORTED_HOSTS) {
    if (rule.match.test(url)) return rule.why;
  }
  return null;
}

async function storageGet(key) {
  try {
    return await chrome.storage.sync.get(key);
  } catch {
    return {};
  }
}

async function storageSet(value) {
  try {
    await chrome.storage.sync.set(value);
  } catch {
    // Storage is a convenience; clipping should not depend on it.
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function safeHostname(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return url || "";
  }
}

function renderActiveTab(tab) {
  activeTab = tab;
  $("favicon").src = tab.favIconUrl || "icons/16.png";
  $("tab-title").textContent = tab.title || tab.url || "(untitled)";
  $("tab-domain").textContent = safeHostname(tab.url);
  const why = restrictedReason(tab.url);
  $("restricted-banner").hidden = !why;
  $("restricted-reason").textContent = why || "";
  setActionsEnabled(!why);
}

function setActionsEnabled(on) {
  $("save").disabled = !on;
  $("copy").disabled = !on;
  $("ai-extract").disabled = !on;
}

async function hasApiKey() {
  try {
    const { googleApiKey } = await chrome.storage.local.get(["googleApiKey"]);
    return Boolean(googleApiKey);
  } catch {
    return false;
  }
}

function openOptions() {
  if (chrome.runtime.openOptionsPage) {
    chrome.runtime.openOptionsPage();
  } else {
    chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
  }
}

function optionsFromForm() {
  return {
    includeImages: $("include-images").checked,
  };
}

function setStatus(text, kind = "") {
  const el = $("status");
  el.textContent = text || "";
  el.className = kind;
}

async function clip(action) {
  if (!activeTab || restrictedReason(activeTab.url)) {
    setStatus("This page can't be clipped.", "error");
    return;
  }

  if (action === "ai-extract" && !(await hasApiKey())) {
    setStatus("Set a Google API key in Settings first.", "error");
    openOptions();
    return;
  }

  const options = optionsFromForm();
  await storageSet({ [PREF_KEY]: options });

  setStatus(action === "copy" ? "Extracting..." : "Clipping...", "");
  setActionsEnabled(false);
  const filename = normalizeDownloadFilename($("filename").value.trim());

  try {
    const resp = await chrome.runtime.sendMessage({
      type: "clip",
      action,
      tabId: activeTab.id,
      filename,
      options,
    });
    if (!resp?.ok) {
      setStatus(resp?.error || "Failed.", "error");
      return;
    }
    if (action === "copy") {
      try {
        await navigator.clipboard.writeText(resp.markdown);
        setStatus("Copied.", "success");
      } catch (e) {
        setStatus(`Extracted but clipboard failed: ${String(e)}`, "error");
      }
    } else {
      const verb = action === "ai-extract" ? "AI-saved" : "Saved";
      setStatus(`${verb}.`, "success");
      setTimeout(() => window.close(), 800);
    }
  } catch (e) {
    setStatus(String(e), "error");
  } finally {
    setActionsEnabled(true);
  }
}

async function applyPrefs() {
  const prefs = (await storageGet(PREF_KEY))[PREF_KEY] || {};
  $("include-images").checked = prefs.includeImages === true;
}

async function init() {
  await applyPrefs();
  const tab = await getActiveTab();
  if (!tab) {
    setStatus("No active tab.", "error");
    return;
  }
  renderActiveTab(tab);
  $("filename").value = defaultFilename(tab.title || safeHostname(tab.url));

  $("save").addEventListener("click", () => clip("save"));
  $("copy").addEventListener("click", () => clip("copy"));
  $("ai-extract").addEventListener("click", () => clip("ai-extract"));
  $("open-options").addEventListener("click", openOptions);
}

init().catch((e) => setStatus(String(e), "error"));

// options.js — settings page for the convert2md extension.
// Stores the Google API key, Gemini model, and per-job concurrency in
// chrome.storage.local. Validates the key on save with a trivial generateContent
// call so users find out immediately if the key is wrong.

import { gemini, validateKey } from "./gemini.js";

const $ = (id) => document.getElementById(id);

const DEFAULTS = {
  googleApiKey: "",
  geminiModel: "gemini-2.5-flash",
  geminiConcurrency: 4,
};

async function loadSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

async function init() {
  const settings = await loadSettings();
  $("api-key").value = settings.googleApiKey;
  $("model").value = settings.geminiModel;
  $("concurrency").value = String(settings.geminiConcurrency);

  $("save").addEventListener("click", save);
  $("clear").addEventListener("click", clearKey);
}

function setStatus(text, kind = "") {
  const el = $("status");
  el.textContent = text || "";
  el.className = kind;
}

async function save() {
  const apiKey = $("api-key").value.trim();
  const model = $("model").value.trim() || DEFAULTS.geminiModel;
  const concurrency = clampInt($("concurrency").value, 1, 32, DEFAULTS.geminiConcurrency);

  if (!apiKey) {
    setStatus("Enter an API key first.", "error");
    return;
  }

  setStatus("Validating…", "");
  $("save").disabled = true;
  try {
    await validateKey({ googleApiKey: apiKey, geminiModel: model });
    await chrome.storage.local.set({
      googleApiKey: apiKey,
      geminiModel: model,
      geminiConcurrency: concurrency,
    });
    setStatus("Saved. Gemini features are ready.", "success");
  } catch (e) {
    setStatus(`Validation failed: ${String(e).slice(0, 200)}`, "error");
  } finally {
    $("save").disabled = false;
  }
}

async function clearKey() {
  await chrome.storage.local.remove(["googleApiKey"]);
  $("api-key").value = "";
  setStatus("Key cleared.", "");
}

function clampInt(value, lo, hi, fallback) {
  const n = Number.parseInt(String(value), 10);
  if (Number.isNaN(n)) return fallback;
  return Math.min(hi, Math.max(lo, n));
}

// Re-export for the SW (so it can call gemini.transcribe with the same module).
export { gemini };

init().catch((e) => setStatus(String(e), "error"));

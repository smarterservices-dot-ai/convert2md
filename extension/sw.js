// sw.js — service worker orchestrator.
//
// Two entry points:
//   - { type: "clip" } from popup → DOM extract (+ optional AI Extract) of the
//     active tab → assembled .md → download or clipboard.
//   - chrome.contextMenus "Transcribe image with convert2md" on right-click
//     of an <img> → fetch via the page's session → Gemini → download .md.
//
// On YouTube pages, AI Extract skips DOM extraction and asks Gemini to
// transcribe the video directly via its native file_uri support.

import {
  assembleDocument,
  blobUrlViaOffscreen,
  defaultFilename,
  isoUtc,
  normalizeDownloadFilename,
  revokeOffscreenUrl,
  toDataUrl,
} from "./md.js";
import { isYouTube, loadPrompt, transcribe, transcribeYouTube } from "./gemini.js";

const VENDOR = [
  "vendor/Readability.js",
  "vendor/Readability-readerable.js",
  "vendor/turndown.js",
  "vendor/turndown-plugin-gfm.js",
];

const DATA_URL_CAP = 1_500_000; // chrome.downloads rejects data URLs beyond ~2MB
const CONTEXT_MENU_ID = "convert2md-transcribe-image";

// ---- Tab-side helpers ------------------------------------------------------

async function clipTab(tabId, options) {
  await chrome.scripting.executeScript({
    target: { tabId, allFrames: false },
    world: "ISOLATED",
    files: [...VENDOR, "extract.js"],
  });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func: (opts) => window.__convert2mdExtract(opts),
    args: [options],
  });
  if (!result) throw new Error("extract returned no result");
  return result;
}

// Fetch an image inside the page's own context — credentials, cookies, and any
// site-specific headers come along for the ride. Returns { base64, mime }.
async function fetchImageInTab(tabId, srcUrl) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func: async (url) => {
      const r = await fetch(url, { credentials: "include" });
      if (!r.ok) throw new Error(`http ${r.status}`);
      const blob = await r.blob();
      const buf = await blob.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = "";
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return { base64: btoa(bin), mime: blob.type || "image/png" };
    },
    args: [srcUrl],
  });
  if (!result) throw new Error("image fetch returned no result");
  return result;
}

function base64ToBlob(base64, mime) {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// Capture the visible viewport of the currently focused tab and return a Blob.
async function captureVisibleViewport() {
  const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: "png" });
  const [, base64] = dataUrl.split(",", 2);
  return base64ToBlob(base64, "image/png");
}

// ---- Settings + prompts ----------------------------------------------------

async function loadGeminiSettings() {
  return await chrome.storage.local.get(["googleApiKey", "geminiModel", "geminiConcurrency"]);
}

async function ensureGeminiReady() {
  const settings = await loadGeminiSettings();
  if (!settings.googleApiKey) {
    return { settings: null, error: "Set a Google API key in Settings first." };
  }
  let prompt;
  try {
    prompt = await loadPrompt("transcribe");
  } catch (e) {
    return { settings: null, error: `Could not load prompt: ${e}` };
  }
  return { settings, prompt };
}

// ---- Output assembly + download / clipboard --------------------------------

async function deliverMarkdown(md, action, filename) {
  if (action === "copy") {
    return { ok: true, markdown: md };
  }
  const useBlob = md.length > DATA_URL_CAP;
  const url = useBlob ? await blobUrlViaOffscreen(md) : toDataUrl(md);
  const safeName = normalizeDownloadFilename(filename);

  let cleanup = () => {};
  if (useBlob) {
    let downloadId = -1;
    const fallback = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(onChanged);
      revokeOffscreenUrl(url);
    }, 60_000);
    const onChanged = (delta) => {
      if (delta.id !== downloadId) return;
      const state = delta.state?.current;
      if (state !== "complete" && state !== "interrupted") return;
      chrome.downloads.onChanged.removeListener(onChanged);
      clearTimeout(fallback);
      revokeOffscreenUrl(url);
    };
    chrome.downloads.onChanged.addListener(onChanged);
    cleanup = (id) => {
      downloadId = id;
    };
  }

  try {
    const id = await chrome.downloads.download({ url, filename: safeName, conflictAction: "uniquify" });
    cleanup(id);
    return { ok: true, id };
  } catch (e) {
    if (useBlob) revokeOffscreenUrl(url);
    return { ok: false, error: String(e) };
  }
}

// ---- Entry point 1: { type: "clip" } from popup ----------------------------

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type !== "clip") return;

  (async () => {
    const action =
      msg.action === "copy" ? "copy" : msg.action === "ai-extract" ? "ai-extract" : "save";
    const options = { includeImages: msg.options?.includeImages === true };
    const tabId = msg.tabId;

    let geminiSettings = null;
    let transcribePrompt = "";
    if (action === "ai-extract") {
      const ready = await ensureGeminiReady();
      if (!ready.settings) {
        reply({ ok: false, error: ready.error });
        return;
      }
      geminiSettings = ready.settings;
      transcribePrompt = ready.prompt;
    }

    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (e) {
      reply({ ok: false, error: String(e) });
      return;
    }

    let data;
    if (action === "ai-extract" && isYouTube(tab.url)) {
      // Skip DOM extraction; the YouTube transcript IS the body.
      try {
        const transcriptMd = await transcribeYouTube(tab.url, geminiSettings, transcribePrompt);
        data = {
          title: tab.title || "YouTube",
          url: tab.url,
          capturedAt: isoUtc(),
          site: safeHostname(tab.url),
          markdown: transcriptMd || "_no transcript_",
          assets: [],
        };
      } catch (e) {
        reply({ ok: false, error: `YouTube transcribe failed: ${String(e)}` });
        return;
      }
    } else {
      try {
        data = await clipTab(tabId, options);
      } catch (e) {
        reply({ ok: false, error: String(e) });
        return;
      }
      if (action === "ai-extract") {
        try {
          const blob = await captureVisibleViewport();
          data.aiVisual = await transcribe(blob, geminiSettings, transcribePrompt);
        } catch (e) {
          // Don't fail the clip if Gemini errors; the DOM body still ships.
          data.aiVisual = `_AI Extract failed: ${String(e).slice(0, 200)}_`;
        }
      }
    }

    const md = assembleDocument([data]);
    reply(await deliverMarkdown(md, action, msg.filename));
  })();

  return true; // keep the message channel open for async reply
});

// ---- Entry point 2: chrome.contextMenus on right-click of an <img> ---------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: CONTEXT_MENU_ID,
    title: "Transcribe image with convert2md",
    contexts: ["image"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== CONTEXT_MENU_ID || !info.srcUrl || !tab?.id) return;

  const ready = await ensureGeminiReady();
  if (!ready.settings) {
    // Open options so the user can drop in an API key.
    if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
    return;
  }

  let blob;
  try {
    const { base64, mime } = await fetchImageInTab(tab.id, info.srcUrl);
    blob = base64ToBlob(base64, mime);
  } catch (e) {
    console.error("convert2md: image fetch failed", e);
    return;
  }

  let markdown;
  try {
    markdown = await transcribe(blob, ready.settings, ready.prompt);
  } catch (e) {
    console.error("convert2md: Gemini transcribe failed", e);
    return;
  }

  const section = {
    title: filenameFromUrl(info.srcUrl) || "Image",
    url: info.srcUrl,
    capturedAt: isoUtc(),
    site: safeHostname(info.srcUrl) || safeHostname(tab.url),
    markdown: markdown || "_no transcription_",
    assets: [],
  };
  const md = assembleDocument([section]);
  await deliverMarkdown(md, "save", defaultFilename(section.title));
});

// ---- Small helpers ---------------------------------------------------------

function safeHostname(url) {
  try {
    return new URL(url).hostname || null;
  } catch {
    return null;
  }
}

function filenameFromUrl(url) {
  try {
    const path = new URL(url).pathname;
    const last = path.split("/").filter(Boolean).pop() || "";
    return last.replace(/\.[a-z0-9]+$/i, "") || null;
  } catch {
    return null;
  }
}

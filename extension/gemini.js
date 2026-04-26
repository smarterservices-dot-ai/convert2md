// gemini.js — minimal, dependency-free REST client for Gemini.
// Mirrors convert2md/gemini.py: same prompts (synced from convert2md/prompts/),
// same model knobs, same generation config (temperature 0, text/plain).
//
// Used by the extension's AI Extract action and the options page (validateKey).

const ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models";

async function blobToBase64(blob) {
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function buildBody(parts) {
  return {
    contents: [{ parts }],
    generationConfig: {
      temperature: 0,
      maxOutputTokens: 8192,
      responseMimeType: "text/plain",
    },
  };
}

async function callGemini(settings, parts) {
  if (!settings?.googleApiKey) throw new Error("missing GOOGLE_API_KEY");
  const model = settings.geminiModel || "gemini-2.5-flash";
  const url = `${ENDPOINT}/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(settings.googleApiKey)}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(buildBody(parts)),
  });
  if (!resp.ok) {
    const errText = await resp.text().catch(() => "");
    throw new Error(`gemini http ${resp.status}: ${errText.slice(0, 200)}`);
  }
  const json = await resp.json();
  const text = json?.candidates?.[0]?.content?.parts?.[0]?.text;
  return (text || "").trim();
}

// --- Public API -------------------------------------------------------------

/**
 * Transcribe one image (Blob) into Markdown using a prompt.
 * Mirrors convert2md.gemini.transcribe (single-call form).
 */
export async function transcribe(blob, settings, prompt) {
  if (!blob) throw new Error("transcribe: missing image blob");
  const mime = blob.type || "image/png";
  const base64 = await blobToBase64(blob);
  return callGemini(settings, [
    { inline_data: { mime_type: mime, data: base64 } },
    { text: prompt || "" },
  ]);
}

/**
 * Ask Gemini to transcribe a YouTube video by URL using its native file_uri
 * support — no transcript scraping, no language list, no JS port of
 * youtube-transcript-api. The model fetches the audio/video itself.
 */
export async function transcribeYouTube(url, settings, prompt) {
  if (!url) throw new Error("transcribeYouTube: missing URL");
  return callGemini(settings, [
    { file_data: { file_uri: url, mime_type: "video/*" } },
    { text: prompt || "" },
  ]);
}

/**
 * Extract a YouTube video id from common URL shapes. Returns null if the URL
 * is not a YouTube watch / shorts / embed / live link.
 */
export function youtubeId(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    if (host === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
    if (host === "youtube.com" || host === "www.youtube.com" || host === "m.youtube.com") {
      if (u.pathname === "/watch") return u.searchParams.get("v");
      const parts = u.pathname.split("/").filter(Boolean);
      if (parts[0] === "shorts" || parts[0] === "embed" || parts[0] === "live") {
        return parts[1] || null;
      }
    }
  } catch {
    return null;
  }
  return null;
}

export function isYouTube(url) {
  return Boolean(youtubeId(url));
}

/**
 * Validate the API key with the smallest possible call.
 * Used by options.js on save.
 */
export async function validateKey(settings) {
  return callGemini(settings, [{ text: "ping" }]);
}

/**
 * Read a synced prompt file shipped at extension/prompts/<name>.md.
 */
export async function loadPrompt(name) {
  const url = chrome.runtime.getURL(`prompts/${name}.md`);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`could not load prompt ${name}.md: ${resp.status}`);
  return await resp.text();
}

// Convenience namespace for callers that prefer a bag-of-functions import.
export const gemini = { transcribe, transcribeYouTube, validateKey, loadPrompt, youtubeId, isYouTube };

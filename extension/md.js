// md.js — shared helpers for the extension side.
//
// Mirrors convert2md/document.py: same frontmatter shape, same placeholder
// rewrite, same timestamp formatter. Tested against the same golden fixtures.

// Canonical ISO-8601 UTC timestamp — second precision, trailing `Z`.
// Mirrors convert2md.document.iso_utc so the two tools round-trip byte-identical
// in the timestamp field. See docs/format.md.
export function isoUtc(d = new Date()) {
  return d.toISOString().replace(/\.\d+Z$/, "Z");
}

// YAML double-quoted scalar escape. Mirrors document.py::yaml_quote.
export function yamlQuote(s) {
  const escaped = String(s)
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r")
    .replace(/\t/g, "\\t");
  return `"${escaped}"`;
}

// Assemble a convert2md document from a list of extracted sections.
// Each section: { title, url, capturedAt, site, markdown, assets: [{mime, base64, alt, src}] }
//
// Single placeholder rewrite rule: `convert2md://asset/N` → `data:<mime>;base64,<b64>`.
// Failure links were already written into the body by extract.js (§5.2).
export function assembleDocument(sections) {
  const q = yamlQuote;
  const out = [
    "---",
    "convert2md: 1",
    `generated_at: ${q(isoUtc())}`,
    `sources: ${sections.length}`,
    "---",
    "",
  ];

  for (const section of sections) {
    out.push("<!-- === SECTION === -->");
    out.push("---");
    out.push(`title: ${q(section.title || section.url || "")}`);
    if (section.url) out.push(`url: ${q(section.url)}`);
    out.push(`source: ${q("extension")}`);
    out.push(`captured_at: ${q(section.capturedAt || isoUtc())}`);
    if (section.site) out.push(`site: ${q(section.site)}`);
    out.push(`images: ${(section.assets || []).length}`);
    if (section.aiVisual) out.push("ai_visual: true");
    out.push("---");
    out.push("");
    out.push(rewritePlaceholders(section.markdown || "", section.assets || []));
    out.push("");
    if (section.aiVisual) {
      out.push("<!-- === VISUAL TRANSCRIPTION === -->");
      out.push("");
      out.push(section.aiVisual.replace(/\s+$/, ""));
      out.push("");
    }
  }

  let text = out.join("\n");
  if (!text.endsWith("\n")) text += "\n";
  return text;
}

export function rewritePlaceholders(body, assets) {
  return body.replace(/convert2md:\/\/asset\/(\d+)/g, (match, idxStr) => {
    const idx = Number(idxStr);
    const asset = assets[idx];
    if (!asset) return match;
    return `data:${asset.mime};base64,${asset.base64}`;
  });
}

// Data URL (used for sub-1.5MB documents; larger go via offscreen Blob).
export function toDataUrl(markdown) {
  const b64 = utf8ToBase64(markdown);
  return `data:text/markdown;charset=utf-8;base64,${b64}`;
}

function utf8ToBase64(str) {
  // Chrome SW has TextEncoder; btoa only handles latin1.
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

// Filename slug used by popup.js for the default filename.
export function slugify(s) {
  return String(s || "clip")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "clip";
}

export function defaultFilename(title) {
  return `${slugify(title)}.md`;
}

export function normalizeDownloadFilename(input, fallback = "convert2md-clip.md") {
  const cleaned = String(input || fallback)
    .replace(/\\/g, "/")
    .split("/")
    .map((part) => sanitizePathPart(part))
    .filter(Boolean)
    .join("/");

  const value = cleaned || fallback;
  if (value.startsWith("/") || value.includes("../") || value === "..") {
    return fallback;
  }
  return value.toLowerCase().endsWith(".md") ? value : `${value}.md`;
}

function sanitizePathPart(part) {
  const cleaned = String(part || "")
    .replace(/[<>:"|?*\x00-\x1f]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^\.+$/, "");
  return cleaned.slice(0, 180);
}

// --- Offscreen Blob path (sw.js helpers; not used in md.test.js) ------------
// Kept here so sw.js imports one module. The offscreen doc is created lazily.

const OFFSCREEN_PATH = "offscreen.html";

async function ensureOffscreen() {
  // Chrome doesn't expose offscreen.hasDocument until 125; we tolerate both.
  if (chrome.offscreen?.hasDocument) {
    if (await chrome.offscreen.hasDocument()) return;
  }
  try {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_PATH,
      reasons: ["BLOBS"],
      justification: "Build a Blob URL for large Markdown downloads.",
    });
  } catch (e) {
    // Already exists (race). Safe to ignore.
    if (!String(e).includes("Only a single offscreen")) throw e;
  }
}

export async function blobUrlViaOffscreen(markdown) {
  await ensureOffscreen();
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type: "md-blob", markdown }, (resp) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      if (!resp?.url) return reject(new Error("offscreen did not return url"));
      resolve(resp.url);
    });
  });
}

export async function revokeOffscreenUrl(url) {
  try {
    await chrome.runtime.sendMessage({ type: "revoke", url });
  } catch {
    // Offscreen may have been torn down; harmless.
  }
}

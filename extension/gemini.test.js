// gemini.test.js — exercise extension/gemini.js against a stubbed fetch.
// No real Gemini call; we just assert request shape and response parsing.

import { test, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { isYouTube, transcribe, transcribeYouTube, validateKey, youtubeId } from "./gemini.js";

let originalFetch;
let lastRequest;
let nextResponse;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  lastRequest = null;
  nextResponse = null;
  globalThis.fetch = async (url, init) => {
    lastRequest = { url, init };
    return nextResponse;
  };
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

test("transcribe POSTs an inline_data image + text part to the right model URL", async () => {
  nextResponse = jsonResponse({
    candidates: [{ content: { parts: [{ text: "  hello world  " }] } }],
  });
  const blob = new Blob([new Uint8Array([1, 2, 3, 4])], { type: "image/png" });
  const out = await transcribe(blob, { googleApiKey: "k", geminiModel: "gemini-2.5-flash" }, "p");

  assert.equal(out, "hello world", "trims whitespace from response text");

  const url = new URL(lastRequest.url);
  assert.ok(url.pathname.endsWith("/models/gemini-2.5-flash:generateContent"));
  assert.equal(url.searchParams.get("key"), "k");
  assert.equal(lastRequest.init.method, "POST");

  const body = JSON.parse(lastRequest.init.body);
  assert.equal(body.contents.length, 1);
  const parts = body.contents[0].parts;
  assert.equal(parts.length, 2);
  assert.equal(parts[0].inline_data.mime_type, "image/png");
  assert.ok(parts[0].inline_data.data);
  assert.equal(parts[1].text, "p");
  assert.equal(body.generationConfig.temperature, 0);
  assert.equal(body.generationConfig.responseMimeType, "text/plain");
});

test("transcribe surfaces HTTP errors with a useful message", async () => {
  nextResponse = jsonResponse({ error: { message: "bad" } }, { ok: false, status: 401 });
  const blob = new Blob([new Uint8Array([0])], { type: "image/png" });
  await assert.rejects(
    () => transcribe(blob, { googleApiKey: "k" }, "p"),
    /gemini http 401/,
  );
});

test("validateKey hits generateContent with a tiny ping payload", async () => {
  nextResponse = jsonResponse({
    candidates: [{ content: { parts: [{ text: "pong" }] } }],
  });
  await validateKey({ googleApiKey: "k", geminiModel: "gemini-2.5-flash" });
  const body = JSON.parse(lastRequest.init.body);
  assert.equal(body.contents[0].parts.length, 1);
  assert.equal(body.contents[0].parts[0].text, "ping");
});

test("transcribe rejects when no API key is provided", async () => {
  const blob = new Blob([new Uint8Array([0])], { type: "image/png" });
  await assert.rejects(
    () => transcribe(blob, { googleApiKey: "" }, "p"),
    /missing GOOGLE_API_KEY/,
  );
});

test("youtubeId extracts ids from common URL shapes", () => {
  assert.equal(youtubeId("https://youtu.be/dQw4w9WgXcQ?t=1"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://www.youtube.com/live/dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://example.com/post"), null);
  assert.equal(isYouTube("https://youtu.be/abc"), true);
  assert.equal(isYouTube("https://example.com"), false);
});

test("transcribeYouTube sends file_data with the YouTube URL", async () => {
  nextResponse = jsonResponse({
    candidates: [{ content: { parts: [{ text: "  full transcript here  " }] } }],
  });
  const out = await transcribeYouTube(
    "https://youtu.be/dQw4w9WgXcQ",
    { googleApiKey: "k", geminiModel: "gemini-2.5-flash" },
    "transcribe this video",
  );
  assert.equal(out, "full transcript here");

  const body = JSON.parse(lastRequest.init.body);
  const parts = body.contents[0].parts;
  assert.equal(parts.length, 2);
  assert.equal(parts[0].file_data.file_uri, "https://youtu.be/dQw4w9WgXcQ");
  assert.equal(parts[0].file_data.mime_type, "video/*");
  assert.equal(parts[1].text, "transcribe this video");
});

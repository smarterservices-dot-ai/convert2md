// md.test.js — exercise md.js pure helpers against the same container
// contract the Python writer enforces.

import { test } from "node:test";
import { strict as assert } from "node:assert";

import {
  assembleDocument,
  defaultFilename,
  isoUtc,
  normalizeDownloadFilename,
  rewritePlaceholders,
  slugify,
  toDataUrl,
  yamlQuote,
} from "./md.js";

test("isoUtc drops milliseconds and preserves trailing Z", () => {
  const d = new Date("2026-04-22T19:30:00.123Z");
  assert.equal(isoUtc(d), "2026-04-22T19:30:00Z");
  const s = isoUtc();
  assert.match(s, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
});

test("yamlQuote escapes quotes, newlines, backslashes", () => {
  assert.equal(yamlQuote("hello"), '"hello"');
  assert.equal(yamlQuote('say "hi"'), '"say \\"hi\\""');
  assert.equal(yamlQuote("a\nb"), '"a\\nb"');
  assert.equal(yamlQuote("a\\b"), '"a\\\\b"');
});

test("rewritePlaceholders substitutes asset indexes", () => {
  const out = rewritePlaceholders(
    "before ![x](convert2md://asset/0) mid ![y](convert2md://asset/1) end",
    [
      { mime: "image/png", base64: "AAAA" },
      { mime: "image/jpeg", base64: "BBBB" },
    ],
  );
  assert.ok(out.includes("data:image/png;base64,AAAA"));
  assert.ok(out.includes("data:image/jpeg;base64,BBBB"));
  assert.ok(!out.includes("convert2md://asset/"));
});

test("rewritePlaceholders leaves unknown indexes intact", () => {
  const out = rewritePlaceholders("![x](convert2md://asset/9)", []);
  assert.ok(out.includes("convert2md://asset/9"));
});

test("assembleDocument emits the container contract", () => {
  const out = assembleDocument([
    {
      title: 'demo "page"',
      url: "https://example.com/a",
      capturedAt: "2026-04-22T19:30:00Z",
      site: "example.com",
      markdown: "# Hello\n\nbody\n",
      assets: [],
    },
    {
      title: "second",
      url: "https://example.com/b",
      capturedAt: "2026-04-22T19:30:05Z",
      site: "example.com",
      markdown: "![fig](convert2md://asset/0)",
      assets: [{ mime: "image/png", base64: "AAAA" }],
    },
  ]);

  assert.ok(out.startsWith("---\nconvert2md: 1\n"));
  assert.ok(out.includes("sources: 2"));
  assert.ok(out.includes("<!-- === SECTION === -->"));
  assert.ok(out.includes('title: "demo \\"page\\""'));
  assert.ok(out.includes('source: "extension"'));
  assert.ok(out.includes('captured_at: "2026-04-22T19:30:00Z"'));
  assert.ok(out.includes('site: "example.com"'));
  assert.ok(out.includes("images: 0"));
  assert.ok(out.includes("images: 1"));
  assert.ok(out.includes("data:image/png;base64,AAAA"));
  assert.ok(out.endsWith("\n"));
});

test("assembleDocument emits a VISUAL TRANSCRIPTION block when aiVisual is set", () => {
  const out = assembleDocument([
    {
      title: "demo",
      url: "https://example.com/a",
      capturedAt: "2026-04-26T12:00:00Z",
      site: "example.com",
      markdown: "# Heading\n\nbody text\n",
      assets: [],
      aiVisual: "# Heading (visual)\n\nA flowchart with three nodes.\n",
    },
  ]);
  assert.ok(out.includes("ai_visual: true"));
  assert.ok(out.includes("<!-- === VISUAL TRANSCRIPTION === -->"));
  assert.ok(out.includes("A flowchart with three nodes."));
  assert.ok(out.indexOf("body text") < out.indexOf("VISUAL TRANSCRIPTION"));
});

test("toDataUrl is base64-encoded UTF-8", () => {
  const url = toDataUrl("héllo");
  assert.match(url, /^data:text\/markdown;charset=utf-8;base64,/);
  const b64 = url.split(",")[1];
  const bin = Buffer.from(b64, "base64").toString("utf-8");
  assert.equal(bin, "héllo");
});

test("slugify + defaultFilename", () => {
  assert.equal(slugify("Hello, World!"), "hello-world");
  assert.equal(slugify(""), "clip");
  assert.equal(defaultFilename("Hello, World!"), "hello-world.md");
  assert.equal(defaultFilename(""), "clip.md");
});

test("normalizeDownloadFilename guards Chrome download paths", () => {
  assert.equal(normalizeDownloadFilename("clips/demo"), "clips/demo.md");
  assert.equal(normalizeDownloadFilename("clips/demo.md"), "clips/demo.md");
  assert.equal(normalizeDownloadFilename("../secret"), "secret.md");
  assert.equal(normalizeDownloadFilename("bad:name?.md"), "bad-name-.md");
  assert.equal(normalizeDownloadFilename(""), "convert2md-clip.md");
});

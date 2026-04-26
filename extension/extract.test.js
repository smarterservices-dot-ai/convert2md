// extract.test.js — run the extension's adapter logic against the same
// Confluence/SharePoint fixtures the Python side uses. happy-dom provides a
// minimal DOM; we stub `location` and `document` before loading extract.js.

import { test, before } from "node:test";
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { Window } from "happy-dom";

const FIXTURES = resolve(import.meta.dirname, "..", "tests", "fixtures");

function loadFixture(name) {
  return readFileSync(resolve(FIXTURES, name), "utf-8");
}

// Build a fresh happy-dom window for each test. Attach the globals extract.js
// expects, then re-import it (invalidate module cache via ?t=ts query).
async function loadExtract(url, html) {
  const win = new Window({ url });
  win.document.write(html);
  // happy-dom 15: `document.implementation.createHTMLDocument` exists.
  const g = globalThis;
  g.window = win;
  g.document = win.document;
  g.location = win.location;
  g.DOMParser = win.DOMParser;
  g.URL = win.URL;
  // extract.js uses an IIFE that registers window.__convert2mdAdapt.
  // Re-evaluate by reading + eval-ing the file (simpler than dynamic import
  // across re-runs with globals swapping).
  const src = readFileSync(resolve(import.meta.dirname, "extract.js"), "utf-8");
  // eslint-disable-next-line no-new-func
  new Function("window", "document", "location", "DOMParser", "URL", src)(
    win,
    win.document,
    win.location,
    win.DOMParser,
    win.URL,
  );
  return win;
}

test("confluence adapter scopes to main-content, maps info macro, keeps drawio img", async () => {
  const html = loadFixture("confluence-page.html");
  const win = await loadExtract("https://tenant.atlassian.net/wiki/spaces/X/pages/1", html);
  const adapted = win.__convert2mdAdapt(win.document);
  const text = adapted.body.textContent;
  assert.ok(text.includes("Architecture Overview"));
  assert.ok(text.includes("⚠️"));
  assert.ok(text.includes("Expanded detail"));
  assert.ok(!text.includes("top nav") || !adapted.body.querySelector("#navigation"));
  // drawio unwrapped to its img
  const imgs = [...adapted.body.querySelectorAll("img")];
  assert.ok(imgs.some((i) => (i.getAttribute("src") || "").includes("arch.png")));
});

test("sharepoint adapter strips header, converts fileviewer to link", async () => {
  const html = loadFixture("sharepoint-page.html");
  const win = await loadExtract("https://tenant.sharepoint.com/sites/x/page.aspx", html);
  const adapted = win.__convert2mdAdapt(win.document);
  const text = adapted.body.textContent;
  assert.ok(text.includes("Incident Runbook"));
  assert.ok(text.includes("runbook.pdf"));
  assert.ok(!text.includes("header (strip)"));
});

test("unknown host is a no-op", async () => {
  const html = "<html><body><p>hello</p></body></html>";
  const win = await loadExtract("https://example.com/post", html);
  const adapted = win.__convert2mdAdapt(win.document);
  assert.equal(adapted, win.document);
});

test("github adapter scopes to article.markdown-body", async () => {
  const html = `
    <html><body>
      <header><nav>top nav</nav></header>
      <script type="application/json" data-target="react-app.embeddedData">{"props":{"contextRegion":{"crumbs":[{"crumb_type":"user"}]}}}</script>
      <main>
        <article class="markdown-body">
          <h1>Free Claude Code</h1>
          <p>real readme</p>
        </article>
      </main>
      <footer>site footer</footer>
    </body></html>
  `;
  const win = await loadExtract("https://github.com/Alishahryar1/free-claude-code/blob/main/README.md", html);
  const adapted = win.__convert2mdAdapt(win.document);
  const text = adapted.body.textContent;
  assert.ok(text.includes("Free Claude Code"));
  assert.ok(text.includes("real readme"));
  assert.ok(!text.includes("top nav"), "nav must be excluded by adapter scope");
  assert.ok(!text.includes("site footer"), "footer must be excluded by adapter scope");
  assert.ok(!text.includes('"crumb_type"'), "embedded JSON props must not bleed in");
});

test("confluence brush param with invalid characters does not crash", async () => {
  const html = `
    <html><body><div id="main-content">
      <div class="code" data-syntaxhighlighter-params='brush: js"; eval();'>
        <pre>console.log(1)</pre>
      </div>
    </div></body></html>
  `;
  const win = await loadExtract("https://tenant.atlassian.net/wiki/x", html);
  // Adapter must complete without throwing despite the malicious brush token.
  const adapted = win.__convert2mdAdapt(win.document);
  const code = adapted.body.querySelector("pre code");
  assert.ok(code, "pre>code wrapper still produced");
  assert.equal(code.className, "", "untrusted brush token must be dropped");
});

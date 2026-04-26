// extract.js — runs in the target tab (ISOLATED world) via executeScript.
// Vendored libs (Readability, Turndown, Turndown-plugin-gfm) are injected
// immediately before this file by sw.js.
//
// One smart-extract pipeline:
//   1. Per-host adapter scopes the document (atlassian, sharepoint, github).
//   2. Scrub page chrome (script/style/nav/footer/aside/aria-hidden/...).
//   3. Readability when confident; else use the scrubbed body directly.
//   4. Turndown with GFM, fenced code language preservation, raw SVG.
//   5. Optional image inlining (off by default — most page images are noise).

(function () {
  if (window.__convert2mdExtract) return; // idempotent

  const SCRUB_SELECTOR = [
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "input",
    "button",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "[role='complementary']",
    "[role='search']",
    "[role='dialog']",
    "[aria-hidden='true']",
    "[hidden]",
    ".cookie-banner",
    ".cookie-consent",
    "#cookieconsent",
    "#cookie-banner",
  ].join(",");

  // Hosts whose images are not useful in LLM context: avatars, tracking pixels,
  // social-network chrome. Skipped even when "Include images" is on.
  const IMAGE_BLOCK_HOSTS = [
    "avatars.githubusercontent.com",
    "secure.gravatar.com",
    "gravatar.com",
    "www.google-analytics.com",
    "stats.g.doubleclick.net",
  ];

  window.__convert2mdExtract = async function (options = {}) {
    const cfg = {
      includeImages: options.includeImages === true,
      minImageBytes: 2_048,
      maxImageBytes: 5_000_000,
    };

    const adapted = (window.__convert2mdAdapt?.(document)) ?? document;
    const scrubbed = scrubChrome(adapted.cloneNode(true));

    const readerable =
      typeof isProbablyReaderable === "function"
        ? isProbablyReaderable(scrubbed, { minContentLength: 200 })
        : true;
    const article = readerable
      ? new Readability(scrubbed.cloneNode(true), { charThreshold: 500 }).parse()
      : null;

    const html = article?.content ?? scrubbed.body.innerHTML;
    const title = article?.title ?? document.title;

    const container = new DOMParser().parseFromString(html, "text/html");
    scrubChrome(container); // catch anything Readability re-introduced

    const candidates = [];
    for (const img of [...container.querySelectorAll("img")]) {
      const rawSrc = imageSource(img);
      if (!rawSrc || isBlockedImage(rawSrc)) {
        img.remove();
        continue;
      }
      let src;
      try {
        src = new URL(rawSrc, location.href).toString();
      } catch {
        img.remove();
        continue;
      }
      if (!cfg.includeImages) {
        img.setAttribute("src", src);
        continue;
      }
      candidates.push({ img, src });
    }

    const assets = [];
    if (cfg.includeImages && candidates.length) {
      const fetched = await Promise.all(candidates.map(({ src }) => tryInline(src, cfg)));
      for (let i = 0; i < candidates.length; i++) {
        const { img, src } = candidates[i];
        const result = fetched[i];
        if (result.ok) {
          assets.push({ mime: result.mime, base64: result.b64 });
          img.setAttribute("src", `convert2md://asset/${assets.length - 1}`);
        } else {
          // Failure → keep the source URL so the LLM can resolve it.
          img.setAttribute("src", src);
        }
      }
    }

    const td = new TurndownService({
      headingStyle: "atx",
      codeBlockStyle: "fenced",
      bulletListMarker: "-",
      emDelimiter: "_",
    });
    if (typeof turndownPluginGfm !== "undefined") {
      td.use(turndownPluginGfm.gfm);
    }
    td.addRule("preserveSvg", {
      filter: "svg",
      replacement: (_c, node) =>
        `\n<!-- convert2md:svg -->\n${node.outerHTML}\n<!-- /convert2md:svg -->\n`,
    });
    td.addRule("fencedLang", {
      filter: ["pre"],
      replacement: (_c, node) => {
        const code = node.querySelector("code");
        const lang = sanitizeLang(
          ((code?.className || "").match(/language-(\S+)/) || [])[1] || "",
        );
        const text = (code?.textContent ?? node.textContent ?? "").replace(/\n$/, "");
        return "\n```" + lang + "\n" + text + "\n```\n";
      },
    });
    const markdown = td.turndown(container.body.innerHTML).trim() + "\n";

    return {
      title,
      url: location.href,
      capturedAt: isoUtc(),
      site: location.hostname,
      markdown,
      assets,
    };
  };

  // ---- Chrome scrubber --------------------------------------------------

  function scrubChrome(root) {
    for (const node of root.querySelectorAll(SCRUB_SELECTOR)) {
      node.remove();
    }
    // Decorative SVGs without aria-label or text content add tokens but no
    // semantic value. Keep SVG that carries information.
    for (const svg of root.querySelectorAll("svg")) {
      const labelled =
        svg.hasAttribute("aria-label") ||
        svg.hasAttribute("aria-labelledby") ||
        svg.querySelector("title, text") ||
        svg.textContent.trim().length > 0;
      if (!labelled) svg.remove();
    }
    return root;
  }

  // ---- Image helpers ----------------------------------------------------

  function imageSource(img) {
    return (
      img.getAttribute("src") ||
      img.getAttribute("data-src") ||
      img.getAttribute("data-original") ||
      img.getAttribute("data-lazy-src") ||
      firstSrcsetUrl(img.getAttribute("srcset") || img.getAttribute("data-srcset") || "")
    );
  }

  function firstSrcsetUrl(srcset) {
    const first = srcset.split(",").map((part) => part.trim()).find(Boolean);
    return first ? first.split(/\s+/)[0] : "";
  }

  function isBlockedImage(rawSrc) {
    if (!rawSrc) return false;
    if (rawSrc.startsWith("data:")) return false;
    try {
      const u = new URL(rawSrc, location.href);
      return IMAGE_BLOCK_HOSTS.includes(u.hostname);
    } catch {
      return false;
    }
  }

  async function tryInline(src, cfg) {
    try {
      const r = await fetch(src, { credentials: "include" });
      if (!r.ok) return { ok: false, why: `http ${r.status}` };
      const blob = await r.blob();
      if (blob.size < cfg.minImageBytes) return { ok: false, why: `undersize ${blob.size} B` };
      if (blob.size > cfg.maxImageBytes) {
        return { ok: false, why: `oversize ${(blob.size / 1e6).toFixed(1)} MB` };
      }
      const mime = blob.type || "image/png";
      return { ok: true, mime, b64: await blobToBase64(blob) };
    } catch (e) {
      return { ok: false, why: `fetch failed: ${String(e).slice(0, 80)}` };
    }
  }

  async function blobToBase64(blob) {
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function isoUtc(d = new Date()) {
    return d.toISOString().replace(/\.\d+Z$/, "Z");
  }

  // ---- Site adapters ----------------------------------------------------

  window.__convert2mdAdapt = function (doc) {
    const host = location.hostname.toLowerCase();
    if (host.endsWith(".atlassian.net")) return adaptConfluence(doc);
    if (host.endsWith(".sharepoint.com")) return adaptSharePoint(doc);
    if (host === "github.com" || host.endsWith(".github.com")) return adaptGitHub(doc);
    return doc;
  };

  function pickFirst(root, selectors) {
    for (const sel of selectors) {
      const n = root.querySelector(sel);
      if (n) return n;
    }
    return null;
  }

  function stripSelectors(root, selectors) {
    for (const sel of selectors) {
      for (const n of root.querySelectorAll(sel)) n.remove();
    }
  }

  function wrapInDocument(node, title) {
    const wrapped = document.implementation.createHTMLDocument(title || "");
    wrapped.body.appendChild(wrapped.importNode(node, true));
    return wrapped;
  }

  function adaptConfluence(doc) {
    const main =
      pickFirst(doc, ["#main-content", "div.wiki-content", '[data-testid="grid"]']) ||
      doc.body;

    stripSelectors(main, [
      "#comments-section",
      "#navigation",
      "#breadcrumb-section",
      ".page-metadata",
      "#likes-and-labels-container",
    ]);

    const infoMap = {
      "confluence-information-macro-information": "ℹ️",
      "confluence-information-macro-warning": "⚠️",
      "confluence-information-macro-note": "🚫",
      "confluence-information-macro-tip": "💡",
      "confluence-information-macro-success": "✅",
    };
    for (const macro of main.querySelectorAll("div.confluence-information-macro")) {
      let emoji = "ℹ️";
      for (const [cls, sym] of Object.entries(infoMap)) {
        if (macro.classList.contains(cls)) {
          emoji = sym;
          break;
        }
      }
      const bq = doc.createElement("blockquote");
      bq.textContent = `${emoji} ${macro.textContent.trim()}`;
      macro.replaceWith(bq);
    }

    for (const expand of main.querySelectorAll("div.expand-container")) {
      const body = expand.querySelector(".expand-content");
      if (body) expand.replaceWith(body);
    }

    for (const codeDiv of main.querySelectorAll("div.code")) {
      const params = codeDiv.getAttribute("data-syntaxhighlighter-params") || "";
      const lang = sanitizeLang(parseBrush(params));
      const pre = codeDiv.querySelector("pre");
      if (!pre) continue;
      let code = pre.querySelector("code");
      if (!code) {
        code = doc.createElement("code");
        code.textContent = pre.textContent;
        pre.textContent = "";
        pre.appendChild(code);
      }
      if (lang) code.classList.add(`language-${lang}`);
      codeDiv.replaceWith(pre);
    }

    for (const drawio of main.querySelectorAll("div.drawioDiagram")) {
      const img = drawio.querySelector("img");
      if (img) drawio.replaceWith(img);
    }

    return wrapInDocument(main, doc.title);
  }

  function parseBrush(params) {
    for (const token of params.split(";")) {
      const [k, v] = token.split(":").map((s) => s.trim());
      if (k === "brush") return v;
    }
    return "";
  }

  // `language-${lang}` becomes a CSS class via classList.add(), which throws
  // on tokens with whitespace or quotes. Restrict to a safe charset.
  function sanitizeLang(value) {
    return /^[a-zA-Z0-9_-]+$/.test(value || "") ? value : "";
  }

  function adaptSharePoint(doc) {
    const main =
      pickFirst(doc, ['[data-automation-id="pageContentArea"]', ".CanvasComponent"]) ||
      doc.body;

    for (const fv of main.querySelectorAll('[data-sp-feature-tag="FileViewer"]')) {
      const a = fv.querySelector("a[href]");
      if (a) {
        const p = doc.createElement("p");
        const link = doc.createElement("a");
        link.href = a.getAttribute("href");
        link.textContent = a.textContent.trim() || "attached file";
        p.appendChild(link);
        fv.replaceWith(p);
      } else {
        fv.remove();
      }
    }

    stripSelectors(main, [
      '[data-automation-id="pageHeader"]',
      '[data-automation-id="commentsWrapper"]',
      '[data-automation-id="pageProperties"]',
      '[data-sp-feature-tag="Navigation"]',
      '[data-sp-feature-tag="Spacer"]',
    ]);

    return wrapInDocument(main, doc.title);
  }

  // GitHub renders README files into <article class="markdown-body">.
  // Issues, PRs, discussions, gists, and code views all expose narrower
  // wrappers — pick the first that exists.
  function adaptGitHub(doc) {
    const main =
      pickFirst(doc, [
        "article.markdown-body",
        ".markdown-body",
        '[itemprop="text"]',         // README payload on file views
        ".js-issue-title ~ *",       // issues / PRs body container
        "main",
      ]) || doc.body;
    return wrapInDocument(main, doc.title);
  }
})();

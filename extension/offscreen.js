// offscreen.js — runs in an offscreen document (reasons: ["BLOBS"]).
// sw.js delegates large-markdown downloads here because service workers
// cannot call URL.createObjectURL.

const urlToBlob = new Map();

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "md-blob") {
    const blob = new Blob([msg.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    urlToBlob.set(url, blob);
    reply({ url });
    return true;
  }
  if (msg?.type === "revoke") {
    const url = msg.url;
    if (urlToBlob.has(url)) {
      URL.revokeObjectURL(url);
      urlToBlob.delete(url);
    }
    reply({ ok: true });
    return true;
  }
});

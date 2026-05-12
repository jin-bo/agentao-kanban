(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});

  async function fetchJSON(url) {
    const r = await fetch(url, {
      headers: { accept: "application/json" },
      cache: "no-store",
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  // Fetch a text body for inline display, capping it at `maxChars`.
  // Returns a cache-entry-shaped object: `{state:"loaded",text,truncated}`
  // or `{state:"error",status,error}` (the FastAPI `detail` string when
  // present — e.g. the 413 "too large" message — else `HTTP <status>`).
  // Network failures still throw; callers wrap that in their own error
  // entry.
  async function fetchTextWithCap(url, maxChars) {
    const r = await fetch(url);
    if (!r.ok) {
      let detail = "";
      try {
        detail = (await r.json()).detail || "";
      } catch (_e) {
        /* non-JSON error body */
      }
      return { state: "error", status: r.status, error: detail || `HTTP ${r.status}` };
    }
    let text = await r.text();
    let truncated = false;
    if (text.length > maxChars) {
      text = text.slice(0, maxChars);
      truncated = true;
    }
    return { state: "loaded", text, truncated };
  }

  ns.fetchJSON = fetchJSON;
  ns.fetchTextWithCap = fetchTextWithCap;
})();

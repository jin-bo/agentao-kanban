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

  ns.fetchJSON = fetchJSON;
})();

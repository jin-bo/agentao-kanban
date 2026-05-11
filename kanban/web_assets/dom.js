(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined) continue;
        if (k === "class") node.className = v;
        else if (k === "dataset") {
          for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
        } else if (k.startsWith("on") && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else node.setAttribute(k, v);
      }
    }
    if (children) {
      for (const c of [].concat(children)) {
        if (c === null || c === undefined || c === false) continue;
        node.appendChild(
          typeof c === "string" ? document.createTextNode(c) : c,
        );
      }
    }
    return node;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  }

  function jumpToSection(id) {
    const node = document.getElementById(id);
    if (node) node.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Common scaffold for the fetch-once detail sections: an <h3> heading,
  // a "Loading..." placeholder while idle/loading, a "Failed to load..."
  // message on error, and otherwise whatever renderBody() returns (a node
  // or an array of nodes; nullish entries are skipped).
  function sectionShell(opts, renderBody) {
    const wrap = el(
      "div",
      { class: opts.className || "card-detail-section", id: opts.id },
      [el("h3", {}, opts.title)],
    );
    if (opts.state === "loading" || opts.state === "idle") {
      wrap.appendChild(el("p", { class: "hint" }, "Loading..."));
      return wrap;
    }
    if (opts.state === "error") {
      wrap.appendChild(
        el(
          "p",
          { class: "hint artifacts-error" },
          `${opts.errorPrefix || "Failed to load"}: ${opts.error}`,
        ),
      );
      return wrap;
    }
    for (const node of [].concat(renderBody())) {
      if (node) wrap.appendChild(node);
    }
    return wrap;
  }

  ns.el = el;
  ns.copyText = copyText;
  ns.jumpToSection = jumpToSection;
  ns.sectionShell = sectionShell;
})();

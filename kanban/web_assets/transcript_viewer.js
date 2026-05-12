(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const { copyText, el, fetchTextWithCap, fmtBytes, fmtTime, sectionShell } = ns;

  // Client-side display cap, independent of the server's 8 MiB inline
  // cap on /traces/{id}/file. A multi-MiB transcript pasted into a <pre>
  // wedges the modal; show the head and point at "open raw" for the rest.
  const TRANSCRIPT_MAX_CHARS = 256 * 1024;

  const expanded = new Set(); // trace_id of expanded rows
  const cache = new Map(); // trace_id -> {state, text?, truncated?, error?, status?}
  let roleFilter = ""; // "" = all roles

  function reset() {
    expanded.clear();
    cache.clear();
    roleFilter = "";
  }

  // Expand the newest transcript by default (this is the traces section's
  // onLoaded hook) so an unclear result is one glance away, not a click.
  function expandNewest(data) {
    const traces = (data && data.traces) || [];
    if (traces.length) expanded.add(traces[0].trace_id);
  }

  function fileUrl(cardId, traceId) {
    return `/api/cards/${encodeURIComponent(cardId)}/traces/${encodeURIComponent(traceId)}/file`;
  }

  async function loadContent(ctx, trace) {
    const id = trace.trace_id;
    cache.set(id, { state: "loading" });
    if (ctx.isCurrent()) ctx.rerender();
    try {
      cache.set(id, await fetchTextWithCap(fileUrl(ctx.cardId, id), TRANSCRIPT_MAX_CHARS));
    } catch (err) {
      cache.set(id, { state: "error", error: err.message });
    }
    if (ctx.isCurrent()) ctx.rerender();
  }

  function renderContentBlock(ctx, trace) {
    const id = trace.trace_id;
    let entry = cache.get(id);
    if (!entry) {
      // First render of a default-expanded row: seed a "loading" entry
      // synchronously (so repeated renders don't queue duplicate loads),
      // then start the fetch off the current render frame.
      cache.set(id, { state: "loading" });
      entry = cache.get(id);
      Promise.resolve().then(() => {
        if (ctx.isCurrent()) loadContent(ctx, trace);
      });
    }
    if (entry.state === "loading") {
      return el("div", { class: "transcript-content" }, [
        el("pre", { class: "transcript-pre" }, "Loading..."),
      ]);
    }
    if (entry.state === "error") {
      if (entry.status === 413) {
        return el("div", { class: "transcript-content" }, [
          el(
            "p",
            { class: "hint artifacts-error" },
            "Transcript is too large for inline view.",
          ),
          trace.path
            ? el("p", { class: "hint" }, ["On disk: ", el("code", {}, trace.path)])
            : null,
          el(
            "a",
            { href: fileUrl(ctx.cardId, id), target: "_blank", rel: "noopener" },
            "open raw in a new tab",
          ),
        ]);
      }
      return el("div", { class: "transcript-content" }, [
        el(
          "pre",
          { class: "transcript-pre artifacts-error" },
          entry.error || "Failed to load",
        ),
      ]);
    }
    const children = [el("pre", { class: "transcript-pre" }, entry.text || "")];
    if (entry.truncated)
      children.push(
        el(
          "div",
          { class: "hint artifact-truncated" },
          "(preview truncated - open raw for the full transcript)",
        ),
      );
    return el("div", { class: "transcript-content" }, children);
  }

  function renderRow(ctx, trace, isLatest) {
    const id = trace.trace_id;
    const isOpen = expanded.has(id);
    const summary = el("summary", {}, [
      el("span", { class: "trace-id" }, id),
      el(
        "span",
        { class: "trace-meta" },
        `${trace.role || "?"} - ${fmtTime(trace.at)} - ${fmtBytes(trace.size)}`,
      ),
      isLatest ? el("span", { class: "trace-latest" }, "latest") : null,
      el(
        "a",
        {
          href: fileUrl(ctx.cardId, id),
          target: "_blank",
          rel: "noopener",
          class: "file-action",
          onclick: (ev) => ev.stopPropagation(),
        },
        "open raw",
      ),
      trace.path
        ? el(
            "button",
            {
              class: "linklike file-action",
              type: "button",
              title: "copy transcript path",
              onclick: (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                copyText(trace.path);
              },
            },
            "copy path",
          )
        : null,
    ]);
    const det = el(
      "details",
      isOpen
        ? { open: "open", class: "transcript-row" }
        : { class: "transcript-row" },
      [summary],
    );
    det.addEventListener("toggle", () => {
      if (det.open) {
        expanded.add(id);
        if (cache.has(id)) ctx.rerender();
        else loadContent(ctx, trace);
      } else {
        expanded.delete(id);
      }
    });
    if (isOpen) det.appendChild(renderContentBlock(ctx, trace));
    return det;
  }

  function renderBody(ctx) {
    const all = (ctx.traces && ctx.traces.traces) || [];
    if (!all.length) {
      return el(
        "p",
        { class: "hint" },
        "(none - full agent transcripts are saved here when a worker runs; the most recent few per role are kept)",
      );
    }
    const latestId = all[0].trace_id;
    const roles = Array.from(
      new Set(all.map((t) => t.role).filter(Boolean)),
    ).sort();
    const nodes = [];
    if (roles.length > 1) {
      // Stale filter (role no longer present after a refresh) falls back
      // to "all" rather than hiding everything.
      if (roleFilter && !roles.includes(roleFilter)) roleFilter = "";
      const select = el(
        "select",
        {
          class: "transcript-role-filter",
          onchange: (ev) => {
            roleFilter = ev.target.value;
            ctx.rerender();
          },
        },
        [
          el("option", { value: "" }, "all roles"),
          ...roles.map((r) =>
            el(
              "option",
              roleFilter === r ? { value: r, selected: "selected" } : { value: r },
              r,
            ),
          ),
        ],
      );
      nodes.push(
        el("div", { class: "transcript-filter-row" }, [
          el("label", {}, ["role: ", select]),
        ]),
      );
    }
    const visible = roleFilter
      ? all.filter((t) => t.role === roleFilter)
      : all;
    const list = el("div", { class: "transcript-list" });
    for (const t of visible) {
      list.appendChild(renderRow(ctx, t, t.trace_id === latestId));
    }
    nodes.push(list);
    return nodes;
  }

  function renderSection(ctx) {
    return sectionShell(
      {
        title: "Transcripts",
        id: "detail-transcripts",
        state: ctx.tracesState,
        error: ctx.tracesError,
      },
      () => renderBody(ctx),
    );
  }

  ns.transcriptViewer = {
    expandNewest,
    renderSection,
    reset,
  };
})();

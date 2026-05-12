(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const {
    copyText,
    el,
    fetchTextWithCap,
    fileKind,
    fmtBytes,
    fmtSnapshotStamp,
    isPreviewableKind,
    previewKey,
  } = ns;

  const ARTIFACT_PREVIEW_MAX_BYTES = 256 * 1024;
  const ARTIFACT_PREVIEW_MAX_CHARS = 64 * 1024;

  const expandedSnapshots = new Set();
  const expandedPreviews = new Set();
  const previewCache = new Map();
  let artifactFilter = "";

  function reset() {
    expandedSnapshots.clear();
    expandedPreviews.clear();
    previewCache.clear();
    artifactFilter = "";
  }

  function expandNewest(data) {
    const snaps = data && data.snapshots;
    if (snaps && snaps.length) expandedSnapshots.add(snaps[0].snapshot);
  }

  function artifactPathMatches(path) {
    const f = artifactFilter.trim().toLowerCase();
    return !f || String(path).toLowerCase().includes(f);
  }

  async function loadPreview(ctx, snapshot, path) {
    const key = previewKey(snapshot, path);
    previewCache.set(key, { state: "loading" });
    if (ctx.isCurrent()) ctx.rerender();
    const url = `/api/cards/${encodeURIComponent(ctx.cardId)}/artifacts/${encodeURIComponent(snapshot)}/file?path=${encodeURIComponent(path)}`;
    try {
      previewCache.set(key, await fetchTextWithCap(url, ARTIFACT_PREVIEW_MAX_CHARS));
    } catch (err) {
      previewCache.set(key, { state: "error", error: err.message });
    }
    if (ctx.isCurrent()) ctx.rerender();
  }

  function renderPreviewBlock(key, path) {
    const ds = { path };
    const entry = previewCache.get(key) || { state: "loading" };
    if (entry.state === "loading")
      return el("li", { class: "artifact-preview-li", dataset: ds }, [
        el("pre", { class: "artifact-preview" }, "Loading..."),
      ]);
    if (entry.state === "error")
      return el("li", { class: "artifact-preview-li", dataset: ds }, [
        el("pre", { class: "artifact-preview artifacts-error" }, entry.error || "Failed to load"),
      ]);
    const children = [el("pre", { class: "artifact-preview" }, entry.text || "")];
    if (entry.truncated)
      children.push(
        el("div", { class: "hint artifact-truncated" }, "(preview truncated - open in a new tab for the full file)"),
      );
    return el("li", { class: "artifact-preview-li", dataset: ds }, children);
  }

  function renderSection(ctx) {
    const wrap = el("div", {
      class: "card-detail-section",
      id: "detail-artifacts",
    }, [el("h3", {}, "Artifacts")]);
    if (ctx.artifactsState === "loading") {
      wrap.appendChild(el("p", { class: "hint" }, "Loading..."));
      return wrap;
    }
    if (ctx.artifactsState === "error") {
      wrap.appendChild(
        el(
          "p",
          { class: "hint artifacts-error" },
          `Failed to load: ${ctx.artifactsError}`,
        ),
      );
      return wrap;
    }
    if (
      ctx.artifactsState !== "loaded" ||
      !ctx.artifacts ||
      !ctx.artifacts.snapshots ||
      ctx.artifacts.snapshots.length === 0
    ) {
      wrap.appendChild(
        el(
          "p",
          { class: "hint" },
          ctx.emptyHint(),
        ),
      );
      return wrap;
    }

    const filterInput = el("input", {
      type: "search",
      class: "artifact-filter",
      placeholder: "filter files...",
      value: artifactFilter,
      oninput: (ev) => {
        artifactFilter = ev.target.value;
        for (const li of wrap.querySelectorAll("li[data-path]")) {
          li.hidden = !artifactPathMatches(li.dataset.path);
        }
      },
    });
    wrap.appendChild(el("div", { class: "artifact-filter-row" }, [filterInput]));
    for (const snap of ctx.artifacts.snapshots) {
      const fileCountLabel =
        snap.truncated && snap.total_file_count
          ? `${snap.file_count} of ${snap.total_file_count} files`
          : `${snap.file_count} file${snap.file_count === 1 ? "" : "s"}`;
      // Human time label parsed from the `artifacts-<utc-stamp>` name,
      // with the raw name kept as secondary text; null => raw name only.
      const stampLabel = fmtSnapshotStamp(snap.snapshot);
      const meta = ` - ${fileCountLabel} - ${fmtBytes(snap.total_bytes)}`;
      const summaryChildren = stampLabel
        ? [
            el("span", { class: "snapshot-time" }, stampLabel),
            el("span", { class: "snapshot-raw" }, snap.snapshot),
            document.createTextNode(meta),
          ]
        : [document.createTextNode(`${snap.snapshot}${meta}`)];
      if (snap.abs_path)
        summaryChildren.push(
          el(
            "button",
            {
              class: "linklike file-action",
              type: "button",
              title: "copy snapshot path",
              onclick: (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                copyText(snap.abs_path);
              },
            },
            "copy path",
          ),
        );
      const summary = el("summary", {}, summaryChildren);
      const list = el("ul", { class: "artifact-files" });
      for (const f of snap.files) {
        const href = `/api/cards/${encodeURIComponent(ctx.cardId)}/artifacts/${encodeURIComponent(snap.snapshot)}/file?path=${encodeURIComponent(f.path)}`;
        const kind = fileKind(f.path);
        const key = previewKey(snap.snapshot, f.path);
        const absPath = snap.abs_path ? `${snap.abs_path}/${f.path}` : null;
        const previewable = isPreviewableKind(kind) && f.size <= ARTIFACT_PREVIEW_MAX_BYTES;
        const isPreviewOpen = expandedPreviews.has(key);
        const row = el(
          "li",
          { dataset: { path: f.path }, hidden: artifactPathMatches(f.path) ? null : "hidden" },
          [
            el("span", { class: `file-kind kind-${kind}` }, kind),
            el("a", { href, target: "_blank", rel: "noopener" }, f.path),
            el("span", { class: "artifact-size" }, fmtBytes(f.size)),
            previewable
              ? el(
                  "button",
                  {
                    class: "linklike file-action",
                    type: "button",
                    onclick: () => {
                      if (expandedPreviews.has(key)) {
                        expandedPreviews.delete(key);
                        ctx.rerender();
                      } else {
                        expandedPreviews.add(key);
                        if (previewCache.has(key)) ctx.rerender();
                        else loadPreview(ctx, snap.snapshot, f.path);
                      }
                    },
                  },
                  isPreviewOpen ? "hide" : "preview",
                )
              : null,
            absPath
              ? el(
                  "button",
                  { class: "linklike file-action", type: "button", title: "copy path", onclick: () => copyText(absPath) },
                  "copy path",
                )
              : null,
          ],
        );
        list.appendChild(row);
        if (isPreviewOpen) {
          const block = renderPreviewBlock(key, f.path);
          if (!artifactPathMatches(f.path)) block.hidden = true;
          list.appendChild(block);
        }
      }
      if (snap.truncated) {
        list.appendChild(
          el(
            "li",
            { class: "artifact-truncated" },
            `(${snap.total_file_count - snap.file_count} more files not shown - copy from disk)`,
          ),
        );
      }
      const isOpen = expandedSnapshots.has(snap.snapshot);
      const det = el(
        "details",
        isOpen
          ? { open: "open", class: "artifact-snap" }
          : { class: "artifact-snap" },
        [summary, list],
      );
      det.addEventListener("toggle", () => {
        if (det.open) expandedSnapshots.add(snap.snapshot);
        else expandedSnapshots.delete(snap.snapshot);
      });
      wrap.appendChild(det);
    }
    return wrap;
  }

  ns.artifactBrowser = {
    expandNewest,
    renderSection,
    reset,
  };
})();

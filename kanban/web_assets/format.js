(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});

  function shortId(id) {
    return (id || "").slice(0, 8);
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString([], { hour12: false });
    } catch (e) {
      return iso;
    }
  }

  function fmtRelTime(epochSeconds) {
    if (epochSeconds === null || epochSeconds === undefined) return "";
    const delta = Math.max(0, Date.now() / 1000 - Number(epochSeconds));
    if (delta < 60) return `${Math.floor(delta)}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return "?";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MiB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  }

  function fileKind(path) {
    const m = /\.([a-z0-9_]+)$/i.exec(path);
    const ext = m ? m[1].toLowerCase() : "";
    if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"].includes(ext))
      return "image";
    if (ext === "json" || ext === "jsonl") return "json";
    if (
      ["log", "txt", "md", "csv", "tsv", "yaml", "yml", "ini", "cfg", "conf", "toml", "env", "rst"].includes(ext)
    )
      return "text";
    if (
      ["py", "js", "ts", "tsx", "jsx", "sh", "bash", "zsh", "rb", "go", "rs", "c", "h", "cpp", "hpp", "java", "kt", "html", "css", "scss", "sql", "xml", "lua", "php"].includes(ext)
    )
      return "code";
    if (ext === "") return "file";
    return "binary";
  }

  function isPreviewableKind(kind) {
    return kind === "text" || kind === "json" || kind === "code";
  }

  function previewKey(snapshot, path) {
    return snapshot + "|" + path;
  }

  ns.shortId = shortId;
  ns.fmtTime = fmtTime;
  ns.fmtRelTime = fmtRelTime;
  ns.fmtBytes = fmtBytes;
  ns.fileKind = fileKind;
  ns.isPreviewableKind = isPreviewableKind;
  ns.previewKey = previewKey;
})();

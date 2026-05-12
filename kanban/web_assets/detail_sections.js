(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const { copyText, el, jumpToSection, sectionShell } = ns;

  const WORKTREE_STATE_COPY = {
    active: "Worktree directory is still active; review in-progress changes.",
    detached: "Directory released; result branch is preserved.",
    missing:
      "Recorded branch no longer resolves; stale metadata likely needs pruning.",
    none: "No worktree was attached to this card.",
    "not-git":
      "Board is not inside a Git repository; worktree isolation is unavailable.",
  };

  function artifactsEmptyHint(result) {
    const state = result && result.worktree && result.worktree.state;
    if (state === "none")
      return "(none - no worktree was attached, so no gitignored deliverables were captured)";
    if (state === "not-git")
      return "(none - board isn't a Git repo, so artifact capture is unavailable)";
    if (state === "active")
      return "(none yet - gitignored worker output is captured here when the worktree is detached)";
    return "(none - the worker wrote no gitignored deliverables; this only covers files git ignores, not code changes)";
  }

  function renderResultSection(state) {
    return sectionShell(
      {
        title: "Result",
        id: "detail-result",
        className: "card-detail-section result-section",
        state: state.resultState,
        error: state.resultError,
        errorPrefix: "Failed to load result",
      },
      () => renderResultBody(state.result || {}),
    );
  }

  function renderResultBody(r) {
    const wt = r.worktree || {};
    const dl = el("dl", { class: "result-dl" });
    const row = (k, v) => {
      if (v === null || v === undefined || v === "") return;
      dl.appendChild(el("dt", {}, k));
      dl.appendChild(el("dd", {}, v));
    };
    row("status", el("span", { class: "badge" }, r.status || "-"));
    if (r.blocked_reason)
      row("blocked", el("span", { class: "blocked-reason" }, r.blocked_reason));
    if (r.summary) row("summary", el("span", {}, r.summary));
    const stateBadge = el(
      "span",
      { class: `badge wt-state wt-${wt.state || "none"}` },
      wt.state || "none",
    );
    row(
      "worktree",
      el("span", {}, [
        stateBadge,
        " ",
        el("span", { class: "hint" }, WORKTREE_STATE_COPY[wt.state] || ""),
      ]),
    );
    if (wt.branch) row("branch", el("code", {}, wt.branch));
    if (wt.path) row("worktree path", el("code", {}, wt.path));
    if (wt.state === "active" || wt.state === "detached") {
      row(
        "changes",
        el(
          "button",
          {
            class: "linklike",
            type: "button",
            onclick: () => jumpToSection("detail-changes"),
          },
          "view diff ->",
        ),
      );
    }
    const outputs = r.outputs || [];
    if (outputs.length) {
      const ul = el("ul", { class: "result-list" });
      for (const o of outputs) ul.appendChild(el("li", {}, el("code", {}, o)));
      row("outputs", ul);
    }
    const artCount = (r.artifacts || []).length;
    row(
      "artifacts",
      artCount
        ? el("button", {
            class: "linklike",
            type: "button",
            onclick: () => jumpToSection("detail-artifacts"),
          }, `${artCount} snapshot${artCount === 1 ? "" : "s"} ->`)
        : el("span", { class: "hint" }, "none"),
    );
    const traceCount = (r.transcripts || []).length;
    row(
      "transcripts",
      traceCount
        ? el(
            "button",
            {
              class: "linklike",
              type: "button",
              onclick: () => jumpToSection("detail-transcripts"),
            },
            `${traceCount} retained ->`,
          )
        : el("span", { class: "hint" }, "none"),
    );
    const steps = r.next_steps || [];
    if (!steps.length) return dl;
    const stepWrap = el("div", { class: "result-next" }, [
      el("div", { class: "result-next-label" }, "Next steps"),
    ]);
    const ul = el("ul", { class: "next-steps" });
    for (const s of steps) {
      if (s.trim().startsWith("(")) {
        ul.appendChild(el("li", { class: "next-comment" }, s));
        continue;
      }
      ul.appendChild(
        el("li", {}, [
          el(
            "code",
            {
              class: "copyable",
              title: "click to copy",
              onclick: () => copyText(s),
            },
            s,
          ),
        ]),
      );
    }
    stepWrap.appendChild(ul);
    return [dl, stepWrap];
  }

  function renderChangesSection(state) {
    return sectionShell(
      {
        title: "Changes",
        id: "detail-changes",
        state: state.diffState,
        error: state.diffError,
      },
      () => {
        const d = state.diff || {};
        if (d.diff === null || d.diff === undefined) {
          const cls = d.state === "missing" ? "hint artifacts-error" : "hint";
          return el("p", { class: cls }, d.message || "(no diff available)");
        }
        const nodes = d.message
          ? [el("p", { class: "hint" }, d.message)]
          : [];
        if (!d.diff.trim()) {
          nodes.push(el("p", { class: "hint" }, "No changes on this branch yet."));
          return nodes;
        }
        nodes.push(el("pre", { class: "diff-body" }, d.diff));
        return nodes;
      },
    );
  }

  ns.detailSections = {
    artifactsEmptyHint,
    renderChangesSection,
    renderResultSection,
  };
})();

(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const { artifactBrowser, detailSections, el, fetchJSON, fmtTime, postJSON, transcriptViewer } =
    ns;

  const STATUSES = ["inbox", "ready", "doing", "review", "done", "blocked"];
  const REQUEUE_TARGETS = ["inbox", "ready"];

  function createDetailModal(options) {
    const onClose = options && options.onClose ? options.onClose : () => {};
    const onMutated =
      options && options.onMutated ? options.onMutated : () => {};
    let selectedCardId = null;
    let writesEnabled = false;
    let claimedIds = new Set();
    // Persisted across the 5s board-poll re-render so a half-typed block
    // reason / picked dropdown isn't wiped under the operator.
    let actionDrafts = {};
    let actionFeedback = null; // { kind: "ok"|"warn"|"error", text }
    let actionBusy = false;
    let modal = null;
    let body = null;
    let closeBtn = null;
    let keydownHandler = null;
    let lastCard = null;
    let artifacts = null;
    let artifactsState = "idle";
    let artifactsError = "";
    let result = null;
    let resultState = "idle";
    let resultError = "";
    let traces = null;
    let tracesState = "idle";
    let tracesError = "";
    let diff = null;
    let diffState = "idle";
    let diffError = "";

    function selectedId() {
      return selectedCardId;
    }

    // Built lazily and mounted on document.body (not inside the column
    // grid) so the 5s board re-render can't disturb it while it's open.
    function ensure() {
      if (modal) return modal;
      body = el("div", { class: "card-detail-body" }, [
        el("p", { class: "card-detail-loading" }, "Loading..."),
      ]);
      closeBtn = el(
        "button",
        {
          type: "button",
          class: "detail-close",
          "aria-label": "Close",
          onclick: () => close(),
        },
        "x",
      );
      const card = el("div", { class: "card card-detail-modal" }, [body]);
      card.addEventListener("click", (ev) => ev.stopPropagation());

      const backdrop = el(
        "div",
        {
          class: "modal-backdrop hidden",
          role: "dialog",
          "aria-modal": "true",
          "aria-label": "Card detail",
          onclick: () => close(),
        },
        [card],
      );
      modal = backdrop;
      document.body.appendChild(modal);
      return modal;
    }

    function render(card) {
      if (!body) return;
      if (!card) {
        body.replaceChildren(
          el("p", { class: "card-detail-loading" }, "Loading..."),
        );
        return;
      }
      const head = el("div", { class: "card-detail-head" }, [
        el("div", { class: "card-detail-title-block" }, [
          el("h2", { class: "card-detail-title" }, card.title || "(untitled)"),
        ]),
        el("div", { class: "card-detail-meta" }, [
          el(
            "span",
            { class: `badge priority-${card.priority || "MEDIUM"}` },
            card.priority || "MED",
          ),
          el("span", { class: "badge" }, card.status || ""),
          card.owner_role
            ? el("span", { class: "badge" }, card.owner_role)
            : null,
          card.rework_iteration && card.rework_iteration > 0
            ? el(
                "span",
                { class: "badge rework" },
                `rework ${card.rework_iteration}`,
              )
            : null,
          closeBtn,
        ]),
      ]);

      const sections = [];
      const actionsSection = renderActionsSection(card);
      if (actionsSection) sections.push(actionsSection);
      sections.push(renderResultSection());
      sections.push(renderArtifactsSection(card.id));
      sections.push(renderChangesSection());
      sections.push(renderTranscriptsSection(card.id));

      if (card.recent_events && card.recent_events.length) {
        const ev = el("ol", { class: "events" });
        for (const e of card.recent_events.slice().reverse()) {
          ev.appendChild(
            el("li", {}, [
              el("span", { class: "event-time" }, fmtTime(e.at)),
              el(
                "span",
                { class: "event-tag" + (e.role ? ` role-${e.role}` : "") },
                e.display_tag || "info",
              ),
              el("span", { class: "event-message" }, e.message || ""),
            ]),
          );
        }
        sections.push(
          el("div", { class: "card-detail-section" }, [
            el("h3", {}, "Recent events"),
            ev,
          ]),
        );
      }

      const dl = el("dl", {});
      const append = (k, v) => {
        if (v === null || v === undefined || v === "") return;
        if (Array.isArray(v) && v.length === 0) return;
        dl.appendChild(el("dt", {}, k));
        dl.appendChild(
          el("dd", {}, Array.isArray(v) ? v.join(", ") || "-" : String(v)),
        );
      };
      append("id", card.id);
      append("priority", card.priority);
      append("status", card.status);
      append("owner", card.owner_role);
      append("profile", card.agent_profile);
      append("rework", card.rework_iteration);
      append("depends_on", card.depends_on);
      append("updated_at", card.updated_at);
      append("created_at", card.created_at);
      sections.push(
        el("div", { class: "card-detail-section" }, [
          el("h3", {}, "Metadata"),
          dl,
        ]),
      );

      if (card.goal) {
        sections.push(
          el("div", { class: "card-detail-section" }, [
            el("h3", {}, "Goal"),
            el("p", { class: "hint" }, card.goal),
          ]),
        );
      }

      if (card.blocked_reason) {
        sections.push(
          el("div", { class: "card-detail-section" }, [
            el("h3", {}, "Blocked reason"),
            el("div", { class: "blocked-reason" }, card.blocked_reason),
          ]),
        );
      }

      if (card.acceptance_criteria && card.acceptance_criteria.length) {
        sections.push(
          el("div", { class: "card-detail-section" }, [
            el("h3", {}, "Acceptance criteria"),
            el(
              "pre",
              {},
              card.acceptance_criteria
                .map((c, i) => `${i + 1}. ${c}`)
                .join("\n"),
            ),
          ]),
        );
      }

      body.replaceChildren(head, ...sections);
    }

    function draft(key, fallback) {
      return actionDrafts[key] !== undefined ? actionDrafts[key] : fallback;
    }

    function statusSelect(key, fallback, disabled, options) {
      const opts = (options || STATUSES).map((s) =>
        el(
          "option",
          { value: s, ...(s === draft(key, fallback) ? { selected: "selected" } : {}) },
          s,
        ),
      );
      return el(
        "select",
        {
          class: "detail-action-input",
          disabled: disabled ? "disabled" : null,
          onchange: (ev) => {
            actionDrafts[key] = ev.target.value;
          },
        },
        opts,
      );
    }

    function actionRow(label, control, button) {
      return el("div", { class: "detail-action-row" }, [
        el("label", { class: "detail-action-label" }, label),
        control,
        button,
      ]);
    }

    function actionButton(text, disabled, onClick) {
      return el(
        "button",
        {
          type: "button",
          class: "detail-action-btn",
          disabled: disabled ? "disabled" : null,
          onclick: onClick,
        },
        text,
      );
    }

    async function submitAction(cardId, path, bodyFn, opts) {
      if (actionBusy) return;
      let body;
      try {
        body = bodyFn();
      } catch (err) {
        actionFeedback = { kind: "error", text: err.message };
        if (lastCard) render(lastCard);
        return;
      }
      actionBusy = true;
      actionFeedback = { kind: "ok", text: "Working..." };
      if (lastCard) render(lastCard);
      let res;
      try {
        res = await postJSON(
          `/api/cards/${encodeURIComponent(cardId)}/${path}`,
          body,
        );
      } catch (err) {
        actionBusy = false;
        actionFeedback = { kind: "error", text: `Request failed: ${err.message}` };
        if (lastCard) render(lastCard);
        return;
      }
      actionBusy = false;
      if (selectedCardId !== cardId) return;
      if (!res.ok) {
        // `message` is the route's stable error envelope; `detail` covers
        // a FastAPI request-validation 422 (different body shape).
        const msg =
          (res.data && (res.data.message || res.data.detail)) ||
          `HTTP ${res.status}`;
        actionFeedback = { kind: "error", text: msg };
        if (lastCard) render(lastCard);
        return;
      }
      const warnings = (res.data && res.data.warnings) || [];
      actionFeedback = warnings.length
        ? { kind: "warn", text: warnings.join(" / ") }
        : { kind: "ok", text: opts && opts.okText ? opts.okText : "Done." };
      if (opts && opts.clearDrafts) {
        for (const k of opts.clearDrafts) delete actionDrafts[k];
      }
      // Refresh the card, reload the lazy sections (a terminal landing may
      // have detached the worktree / changed result/artifacts/diff state),
      // and nudge the board since the status changed.
      refresh();
      for (const key of Object.keys(detailSectionsToLoad)) {
        loadSection(cardId, key);
      }
      onMutated();
    }

    function renderActionsSection(card) {
      if (!writesEnabled) return null;
      const claimed = claimedIds.has(card.id);
      const children = [];
      if (claimed) {
        children.push(
          el(
            "p",
            { class: "hint detail-action-note" },
            "This card has a live execution claim — actions are disabled until it finishes.",
          ),
        );
      }
      const disabled = claimed || actionBusy;

      // Move
      const moveSel = statusSelect("moveStatus", card.status || "inbox", disabled);
      children.push(
        actionRow(
          "Move to",
          moveSel,
          actionButton("Move", disabled, () =>
            submitAction(
              card.id,
              "move",
              () => ({ status: moveSel.value }),
              { okText: "Moved." },
            ),
          ),
        ),
      );

      // Requeue
      const requeueSel = statusSelect(
        "requeueTarget",
        "inbox",
        disabled,
        REQUEUE_TARGETS,
      );
      const requeueNote = el("input", {
        type: "text",
        class: "detail-action-input",
        placeholder: "note (optional)",
        value: draft("requeueNote", ""),
        disabled: disabled ? "disabled" : null,
        oninput: (ev) => {
          actionDrafts.requeueNote = ev.target.value;
        },
      });
      children.push(
        el("div", { class: "detail-action-row" }, [
          el("label", { class: "detail-action-label" }, "Requeue to"),
          requeueSel,
          requeueNote,
          actionButton("Requeue", disabled, () =>
            submitAction(
              card.id,
              "requeue",
              () => ({
                target: requeueSel.value,
                note: requeueNote.value.trim() || null,
              }),
              { okText: "Requeued.", clearDrafts: ["requeueNote"] },
            ),
          ),
        ]),
      );

      // Block
      const blockReason = el("input", {
        type: "text",
        class: "detail-action-input detail-action-grow",
        placeholder: "block reason",
        value: draft("blockReason", ""),
        disabled: disabled ? "disabled" : null,
        oninput: (ev) => {
          actionDrafts.blockReason = ev.target.value;
        },
      });
      children.push(
        actionRow(
          "Block",
          blockReason,
          actionButton("Block", disabled, () =>
            submitAction(
              card.id,
              "block",
              () => {
                const reason = blockReason.value.trim();
                if (!reason) throw new Error("block reason must not be blank");
                return { reason };
              },
              { okText: "Blocked.", clearDrafts: ["blockReason"] },
            ),
          ),
        ),
      );

      // Unblock
      const unblockSel = statusSelect("unblockTarget", "inbox", disabled);
      children.push(
        actionRow(
          "Unblock to",
          unblockSel,
          actionButton("Unblock", disabled, () =>
            submitAction(
              card.id,
              "unblock",
              () => ({ target: unblockSel.value }),
              { okText: "Unblocked." },
            ),
          ),
        ),
      );

      if (actionFeedback) {
        children.push(
          el(
            "p",
            { class: `detail-action-feedback feedback-${actionFeedback.kind}` },
            actionFeedback.text,
          ),
        );
      }

      return el("div", { class: "card-detail-section card-detail-actions" }, [
        el("h3", {}, "Actions"),
        ...children,
      ]);
    }

    function renderResultSection() {
      return detailSections.renderResultSection({
        result,
        resultError,
        resultState,
      });
    }

    function renderArtifactsSection(cardId) {
      return artifactBrowser.renderSection({
        cardId,
        artifacts,
        artifactsError,
        artifactsState,
        emptyHint: () =>
          resultState === "loaded"
            ? detailSections.artifactsEmptyHint(result)
            : "(none - gitignored worker output is captured here when a worktree is detached)",
        isCurrent: () => selectedCardId === cardId && !!lastCard,
        rerender: () => {
          if (lastCard) render(lastCard);
        },
      });
    }

    function renderChangesSection() {
      return detailSections.renderChangesSection({
        diff,
        diffError,
        diffState,
      });
    }

    function renderTranscriptsSection(cardId) {
      return transcriptViewer.renderSection({
        cardId,
        traces,
        tracesError,
        tracesState,
        isCurrent: () => selectedCardId === cardId && !!lastCard,
        rerender: () => {
          if (lastCard) render(lastCard);
        },
      });
    }

    const detailSectionsToLoad = {
      result: {
        url: (id) => `/api/cards/${encodeURIComponent(id)}/result`,
        set: (data, state, error) => {
          result = data;
          resultState = state;
          resultError = error;
        },
      },
      artifacts: {
        url: (id) => `/api/cards/${encodeURIComponent(id)}/artifacts`,
        set: (data, state, error) => {
          artifacts = data;
          artifactsState = state;
          artifactsError = error;
        },
        onLoaded: artifactBrowser.expandNewest,
      },
      traces: {
        url: (id) => `/api/cards/${encodeURIComponent(id)}/traces`,
        set: (data, state, error) => {
          traces = data;
          tracesState = state;
          tracesError = error;
        },
        onLoaded: transcriptViewer.expandNewest,
      },
      diff: {
        url: (id) => `/api/cards/${encodeURIComponent(id)}/diff`,
        set: (data, state, error) => {
          diff = data;
          diffState = state;
          diffError = error;
        },
      },
    };

    async function loadSection(id, key) {
      const spec = detailSectionsToLoad[key];
      const rerenderIfCurrent = () => {
        if (selectedCardId === id && lastCard) render(lastCard);
      };
      spec.set(null, "loading", "");
      rerenderIfCurrent();
      try {
        const data = await fetchJSON(spec.url(id));
        if (selectedCardId !== id) return;
        spec.set(data, "loaded", "");
        if (spec.onLoaded) spec.onLoaded(data);
      } catch (err) {
        if (selectedCardId !== id) return;
        spec.set(null, "error", err.message);
      }
      rerenderIfCurrent();
    }

    async function refresh() {
      if (!selectedCardId || !modal) return;
      try {
        const card = await fetchJSON(
          `/api/cards/${encodeURIComponent(selectedCardId)}`,
        );
        if (selectedCardId !== card.id) return;
        lastCard = card;
        render(card);
      } catch (err) {
        if (!body) return;
        body.replaceChildren(
          el(
            "p",
            { class: "card-detail-loading" },
            `Failed to load: ${err.message}`,
          ),
        );
      }
    }

    function clearSections(stateValue) {
      lastCard = null;
      artifacts = null;
      artifactsState = stateValue;
      artifactsError = "";
      result = null;
      resultState = stateValue;
      resultError = "";
      traces = null;
      tracesState = stateValue;
      tracesError = "";
      diff = null;
      diffState = stateValue;
      diffError = "";
      actionDrafts = {};
      actionFeedback = null;
      actionBusy = false;
      artifactBrowser.reset();
      transcriptViewer.reset();
    }

    function open(id) {
      selectedCardId = id;
      clearSections("loading");
      const active = ensure();
      active.classList.remove("hidden");
      if (!keydownHandler) {
        keydownHandler = (ev) => {
          if (ev.key === "Escape") close();
        };
        document.addEventListener("keydown", keydownHandler);
      }
      render(null);
      refresh();
      for (const key of Object.keys(detailSectionsToLoad)) loadSection(id, key);
    }

    function close() {
      selectedCardId = null;
      clearSections("idle");
      if (modal) modal.classList.add("hidden");
      if (keydownHandler) {
        document.removeEventListener("keydown", keydownHandler);
        keydownHandler = null;
      }
      onClose();
    }

    function setWritesEnabled(value) {
      const next = !!value;
      if (next === writesEnabled) return;
      writesEnabled = next;
      if (lastCard) render(lastCard);
    }

    function setClaimedCardIds(ids) {
      const next = new Set(ids || []);
      // Only re-render if the open card's claim state actually flipped.
      const wasClaimed = selectedCardId ? claimedIds.has(selectedCardId) : false;
      const isClaimed = selectedCardId ? next.has(selectedCardId) : false;
      claimedIds = next;
      if (wasClaimed !== isClaimed && lastCard) render(lastCard);
    }

    return {
      close,
      open,
      refresh,
      selectedId,
      setWritesEnabled,
      setClaimedCardIds,
    };
  }

  ns.createDetailModal = createDetailModal;
})();

(function () {
  "use strict";

  const config = window.KANBAN_CONFIG || { pollIntervalMs: 5000 };
  const pollMs = Math.max(Number(config.pollIntervalMs) || 5000, 250);

  const eventsEl = document.getElementById("events");
  const runtimeEl = document.getElementById("runtime");
  const statusDot = document.getElementById("status-dot");
  const updatedAtEl = document.getElementById("updated-at");
  const boardDirEl = document.getElementById("board-dir");

  let selectedCardId = null;
  // Modals live on document.body, not inside the column grid, so the
  // 5s board poll can't touch them while the user is typing or reading.
  // Built lazily on first open. Trigger nodes are rebuilt per render —
  // cheap, no state.
  let addCardModal = null;
  let addCardKeydownHandler = null;
  let detailModal = null;
  let detailModalBody = null;
  let detailKeydownHandler = null;
  let writesEnabled = false;
  // Artifacts are fetched once per detail-modal open (not on every 5s
  // tick) — listings can be large and don't change while the modal is
  // open under any normal workflow.
  let detailLastCard = null;
  let detailArtifacts = null;
  let detailArtifactsState = "idle"; // idle | loading | loaded | error
  let detailArtifactsError = "";
  // The unified "result" view (status, summary, worktree state, artifact
  // & transcript counts, next steps) — same source the CLI `kanban result`
  // reads. Fetched once per detail-modal open, like artifacts.
  let detailResult = null;
  let detailResultState = "idle"; // idle | loading | loaded | error
  let detailResultError = "";
  // Retained raw agent transcripts ("traces" on the API, "Transcripts" in
  // the UI). Fetched once per detail-modal open, like artifacts.
  let detailTraces = null;
  let detailTracesState = "idle"; // idle | loading | loaded | error
  let detailTracesError = "";
  // Tracks which artifact snapshots the user has expanded. The detail
  // modal re-renders on every 5s tick, so without this state any
  // <details> the user opened would collapse back to default.
  const expandedSnapshots = new Set();
  let lastBoardCards = [];

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

  function setStatus(kind) {
    statusDot.classList.remove("idle", "live", "error");
    statusDot.classList.add(kind);
  }

  function shortId(id) {
    return (id || "").slice(0, 8);
  }

  function fmtTime(iso) {
    if (!iso) return "–";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString([], { hour12: false });
    } catch (e) {
      return iso;
    }
  }

  function renderBoard(data) {
    boardDirEl.textContent = data.board_dir || "";
    writesEnabled = !!data.writes_enabled;
    // Excludes DONE cards because depending on a closed card is rarely
    // intentional. The API still accepts them if a full id is pasted.
    lastBoardCards = (data.columns || [])
      .filter((c) => c.status !== "done")
      .flatMap((c) => c.cards || []);
    for (const col of data.columns) {
      const slot = document.getElementById(`col-${col.status}`);
      if (!slot) continue;
      const head = el("div", { class: "column-head" }, [
        col.title,
        el("span", { class: "count" }, String(col.count)),
      ]);
      const body = el("div", { class: "column-body" });
      for (const card of col.cards) body.appendChild(renderCard(card));
      // Inbox column gets a "+ Add card" trigger when writes are on.
      // The actual form lives in a modal mounted to document.body so
      // the poll can't disrupt it while the user is typing.
      if (col.status === "inbox" && writesEnabled) {
        slot.replaceChildren(head, makeAddCardTrigger(), body);
      } else {
        slot.replaceChildren(head, body);
      }
    }
  }

  function makeAddCardTrigger() {
    return el(
      "button",
      {
        type: "button",
        class: "add-toggle",
        onclick: openAddCardModal,
        "aria-label": "Add card",
      },
      "+ Add card",
    );
  }

  function ensureAddCardModal() {
    if (addCardModal) return addCardModal;
    const titleInput = el("input", {
      type: "text",
      name: "title",
      placeholder: "Title (required)",
      maxlength: "500",
      required: "required",
      class: "add-input add-title",
    });
    const goalInput = el("textarea", {
      name: "goal",
      placeholder: "Goal — what does done look like?",
      rows: "3",
      class: "add-input",
    });
    const prioritySelect = el(
      "select",
      { name: "priority", class: "add-input add-priority" },
      ["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((p) =>
        el(
          "option",
          { value: p, ...(p === "MEDIUM" ? { selected: "selected" } : {}) },
          p,
        ),
      ),
    );
    const acceptanceInput = el("textarea", {
      name: "acceptance",
      placeholder: "Acceptance criteria (one per line)",
      rows: "3",
      class: "add-input",
    });

    // Match by full id or unique short-id prefix.
    const selectedDeps = new Set();
    const dependsListEl = el("ul", { class: "depends-chips" });
    const dependsListId = "kanban-depends-options";
    const dependsDatalist = el("datalist", { id: dependsListId });
    const dependsInput = el("input", {
      type: "text",
      class: "add-input depends-input",
      list: dependsListId,
      placeholder: "Type title or paste card id, then Enter",
      autocomplete: "off",
    });
    const dependsAddBtn = el(
      "button",
      { type: "button", class: "depends-add" },
      "+ Add",
    );

    function refreshDependsOptions() {
      const opts = lastBoardCards
        .filter((c) => !selectedDeps.has(c.id))
        .map((c) =>
          el(
            "option",
            { value: c.id },
            `${shortId(c.id)} — ${c.title || "(untitled)"} [${c.status}]`,
          ),
        );
      dependsDatalist.replaceChildren(...opts);
    }

    function renderDepsChips() {
      const chips = Array.from(selectedDeps).map((id) => {
        const meta = lastBoardCards.find((c) => c.id === id);
        const label = meta
          ? `${shortId(id)} — ${meta.title || "(untitled)"}`
          : shortId(id);
        return el("li", { class: "depends-chip", dataset: { dep: id } }, [
          el("span", {}, label),
          el(
            "button",
            {
              type: "button",
              class: "depends-remove",
              "aria-label": `Remove ${label}`,
              onclick: () => {
                selectedDeps.delete(id);
                renderDepsChips();
                refreshDependsOptions();
              },
            },
            "×",
          ),
        ]);
      });
      dependsListEl.replaceChildren(...chips);
    }

    function tryAddDep() {
      const raw = dependsInput.value.trim();
      if (!raw) return;
      let match = lastBoardCards.find((c) => c.id === raw);
      if (!match) {
        const prefixed = lastBoardCards.filter((c) => c.id.startsWith(raw));
        if (prefixed.length === 1) match = prefixed[0];
      }
      if (!match) {
        errorEl.textContent = `No card matches "${raw}" — pick from the autocomplete list, or paste a full id from \`kanban list\`.`;
        return;
      }
      if (selectedDeps.has(match.id)) {
        // Silent no-op for re-adds — chip already visible.
        dependsInput.value = "";
        return;
      }
      selectedDeps.add(match.id);
      dependsInput.value = "";
      errorEl.textContent = "";
      renderDepsChips();
      refreshDependsOptions();
    }

    dependsInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        // Stop the form's submit handler from firing on Enter inside
        // the depends search box.
        ev.preventDefault();
        tryAddDep();
      }
    });
    dependsInput.addEventListener("change", () => {
      // Datalist selection lands as a "change" event with value=full
      // uuid. Auto-add so the user doesn't also have to click "+ Add".
      const v = dependsInput.value.trim();
      if (v && lastBoardCards.some((c) => c.id === v)) {
        tryAddDep();
      }
    });
    dependsAddBtn.addEventListener("click", tryAddDep);

    const dependsRow = el("div", { class: "depends-row" }, [
      dependsInput,
      dependsAddBtn,
    ]);

    const errorEl = el("div", { class: "add-error", role: "alert" });
    const submitBtn = el(
      "button",
      { type: "submit", class: "add-submit" },
      "Create",
    );
    const cancelBtn = el(
      "button",
      { type: "button", class: "add-cancel" },
      "Cancel",
    );
    const headerRow = el("div", { class: "card-modal-head" }, [
      el("span", { class: "badge card-id" }, "new"),
      prioritySelect,
    ]);
    const cardForm = el(
      "form",
      { class: "card card-modal", novalidate: "novalidate" },
      [
        headerRow,
        titleInput,
        el("label", { class: "add-label" }, "Goal"),
        goalInput,
        el("label", { class: "add-label" }, "Acceptance criteria"),
        acceptanceInput,
        el("label", { class: "add-label" }, "Depends on (optional)"),
        dependsListEl,
        dependsRow,
        dependsDatalist,
        errorEl,
        el("div", { class: "add-actions" }, [cancelBtn, submitBtn]),
      ],
    );
    // Stop backdrop click-to-close from firing when interacting inside
    // the card itself. Without this, clicking on a label or empty area
    // inside the form bubbles up and closes the modal.
    cardForm.addEventListener("click", (ev) => ev.stopPropagation());

    const backdrop = el(
      "div",
      {
        class: "modal-backdrop hidden",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": "Add card",
        onclick: () => closeAddCardModal(),
      },
      [cardForm],
    );

    function reset() {
      titleInput.value = "";
      goalInput.value = "";
      prioritySelect.value = "MEDIUM";
      acceptanceInput.value = "";
      dependsInput.value = "";
      selectedDeps.clear();
      renderDepsChips();
      refreshDependsOptions();
      errorEl.textContent = "";
    }

    cancelBtn.addEventListener("click", () => closeAddCardModal());

    cardForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errorEl.textContent = "";
      const title = titleInput.value.trim();
      if (!title) {
        errorEl.textContent = "Title is required.";
        titleInput.focus();
        return;
      }
      const acceptance = acceptanceInput.value
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const dependsOn = Array.from(selectedDeps);
      submitBtn.disabled = true;
      submitBtn.textContent = "Creating…";
      try {
        const r = await fetch("/api/cards", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "application/json",
          },
          body: JSON.stringify({
            title,
            goal: goalInput.value.trim(),
            priority: prioritySelect.value,
            acceptance_criteria: acceptance,
            depends_on: dependsOn,
          }),
        });
        if (!r.ok) {
          let detail = `${r.status} ${r.statusText}`;
          try {
            const data = await r.json();
            if (data && data.detail) detail = String(data.detail);
          } catch (e) {
            /* keep status text */
          }
          errorEl.textContent = detail;
          return;
        }
        closeAddCardModal();
        // Pull fresh state immediately so the new card appears without
        // waiting for the next scheduled tick.
        tick();
      } catch (err) {
        errorEl.textContent = `Network error: ${err.message}`;
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Create";
      }
    });

    backdrop._reset = reset;
    backdrop._titleInput = titleInput;
    backdrop._refreshDeps = refreshDependsOptions;
    addCardModal = backdrop;
    document.body.appendChild(addCardModal);
    return addCardModal;
  }

  function openAddCardModal() {
    const modal = ensureAddCardModal();
    modal.classList.remove("hidden");
    // Repopulate the depends_on dropdown from the latest tick — cards
    // created since the modal was last opened should show up without
    // a manual reload.
    if (modal._refreshDeps) modal._refreshDeps();
    // Defer focus so the show transition doesn't fight the cursor.
    setTimeout(() => modal._titleInput.focus(), 0);
    if (!addCardKeydownHandler) {
      addCardKeydownHandler = (ev) => {
        if (ev.key === "Escape") closeAddCardModal();
      };
      document.addEventListener("keydown", addCardKeydownHandler);
    }
  }

  function closeAddCardModal() {
    if (!addCardModal) return;
    addCardModal.classList.add("hidden");
    addCardModal._reset();
    if (addCardKeydownHandler) {
      document.removeEventListener("keydown", addCardKeydownHandler);
      addCardKeydownHandler = null;
    }
  }

  function renderCard(card) {
    const meta = [
      el(
        "span",
        { class: `badge priority-${card.priority || "MEDIUM"}` },
        card.priority || "MED",
      ),
      el("span", { class: "badge card-id" }, shortId(card.id)),
    ];
    if (card.owner_role)
      meta.push(el("span", { class: "badge" }, card.owner_role));
    if (card.rework_iteration && card.rework_iteration > 0)
      meta.push(
        el("span", { class: "badge rework" }, `rework ${card.rework_iteration}`),
      );
    if (card.agent_profile)
      meta.push(el("span", { class: "badge" }, card.agent_profile));

    const children = [
      el("div", { class: "card-title" }, card.title || "(untitled)"),
      el("div", { class: "card-meta" }, meta),
    ];
    if (card.status === "blocked" && card.blocked_reason) {
      children.push(
        el("div", { class: "blocked-reason" }, card.blocked_reason),
      );
    }
    const node = el(
      "div",
      {
        class: "card" + (card.id === selectedCardId ? " selected" : ""),
        dataset: { cardId: card.id },
        onclick: () => selectCard(card.id),
      },
      children,
    );
    return node;
  }

  function renderEvents(events) {
    eventsEl.replaceChildren(
      ...events
        .slice()
        .reverse()
        .map((ev) =>
          el("li", {}, [
            el("span", { class: "event-time" }, fmtTime(ev.at)),
            el(
              "span",
              {
                class:
                  "event-tag" + (ev.role ? ` role-${ev.role}` : ""),
              },
              ev.display_tag || "info",
            ),
            el(
              "span",
              { class: "event-message" },
              [shortId(ev.card_id), " ", ev.message || ""].join(""),
            ),
          ]),
        ),
    );
  }

  function fmtRelTime(epochSeconds) {
    if (epochSeconds === null || epochSeconds === undefined) return "";
    const delta = Math.max(0, Date.now() / 1000 - Number(epochSeconds));
    if (delta < 60) return `${Math.floor(delta)}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function renderDaemon(daemon) {
    if (!daemon) return null;
    const status = daemon.status || "stopped";
    const detail = [];
    if (daemon.pid) detail.push(`pid ${daemon.pid}`);
    if (daemon.started_at)
      detail.push(`started ${fmtRelTime(daemon.started_at)}`);
    const labelByStatus = {
      running: "running",
      stopped: "stopped",
      stale: "stale lock",
    };
    return el("div", { class: "runtime-block daemon-block" }, [
      el("h3", {}, "Daemon"),
      el("div", { class: `daemon-row daemon-${status}` }, [
        el("span", { class: "daemon-dot" }),
        el("span", { class: "daemon-label" }, labelByStatus[status] || status),
        detail.length
          ? el("span", { class: "daemon-detail" }, detail.join(", "))
          : null,
      ]),
    ]);
  }

  function renderRuntime(runtime, daemon) {
    const blocks = [];
    const daemonBlock = renderDaemon(daemon);
    if (daemonBlock) blocks.push(daemonBlock);
    const claims = runtime.claims || [];
    const workers = runtime.workers || [];
    blocks.push(
      el("div", { class: "runtime-block" }, [
        el("h3", {}, `Claims (${claims.length})`),
        claims.length === 0
          ? el("div", { class: "runtime-empty" }, "none")
          : el(
              "div",
              {},
              claims.map((c) =>
                el("div", { class: "runtime-row" }, [
                  el(
                    "span",
                    {},
                    `${shortId(c.card_id)} ${c.role}` +
                      (c.worker_id ? ` → ${c.worker_id}` : " (unassigned)"),
                  ),
                  el("span", {}, fmtTime(c.heartbeat_at)),
                ]),
              ),
            ),
      ]),
    );
    blocks.push(
      el("div", { class: "runtime-block" }, [
        el("h3", {}, `Workers (${workers.length})`),
        workers.length === 0
          ? el("div", { class: "runtime-empty" }, "none")
          : el(
              "div",
              {},
              workers.map((w) =>
                el("div", { class: "runtime-row" }, [
                  el("span", {}, `${w.worker_id} (pid ${w.pid})`),
                  el("span", {}, fmtTime(w.heartbeat_at)),
                ]),
              ),
            ),
      ]),
    );
    runtimeEl.replaceChildren(...blocks);
  }

  function ensureDetailModal() {
    if (detailModal) return detailModal;
    detailModalBody = el("div", { class: "card-detail-body" }, [
      el("p", { class: "card-detail-loading" }, "Loading…"),
    ]);
    const closeBtn = el(
      "button",
      {
        type: "button",
        class: "detail-close",
        "aria-label": "Close",
        onclick: () => closeDetailModal(),
      },
      "×",
    );
    const card = el("div", { class: "card card-detail-modal" }, [
      detailModalBody,
      // Close button is positioned via the inner head row that
      // renderDetailInto rebuilds — we insert it there at render time.
    ]);
    card.addEventListener("click", (ev) => ev.stopPropagation());
    card._closeBtn = closeBtn;

    const backdrop = el(
      "div",
      {
        class: "modal-backdrop hidden",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": "Card detail",
        onclick: () => closeDetailModal(),
      },
      [card],
    );
    backdrop._card = card;
    detailModal = backdrop;
    document.body.appendChild(detailModal);
    return detailModal;
  }

  function renderDetailInto(card) {
    if (!detailModalBody) return;
    if (!card) {
      detailModalBody.replaceChildren(
        el("p", { class: "card-detail-loading" }, "Loading…"),
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
        detailModal && detailModal._card._closeBtn,
      ]),
    ]);

    // Result and Artifacts lead so the operator can answer "what did this
    // card produce / where is it" before wading through metadata.
    const sections = [];

    sections.push(renderResultSection());
    sections.push(renderArtifactsSection(card.id));
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
        el("dd", {}, Array.isArray(v) ? v.join(", ") || "–" : String(v)),
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

    detailModalBody.replaceChildren(head, ...sections);
  }

  // Operator-facing copy for each worktree state, mirroring the table in
  // docs/kanban-web-ui-result-improvement-plan.md and the CLI vocabulary.
  const WORKTREE_STATE_COPY = {
    active: "Worktree directory is still active; review in-progress changes.",
    detached: "Directory released; result branch is preserved.",
    missing:
      "Recorded branch no longer resolves; stale metadata likely needs pruning.",
    none: "No worktree was attached to this card.",
    "not-git":
      "Board is not inside a Git repository; worktree isolation is unavailable.",
  };

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  }

  function jumpToSection(id) {
    const node = document.getElementById(id);
    if (node) node.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function artifactsEmptyHint(result) {
    // Explain *why* there are no artifacts based on the result state,
    // rather than a flat "(none)".
    const state = result && result.worktree && result.worktree.state;
    if (state === "none")
      return "(none — no worktree was attached, so no gitignored deliverables were captured)";
    if (state === "not-git")
      return "(none — board isn't a Git repo, so artifact capture is unavailable)";
    if (state === "active")
      return "(none yet — gitignored worker output is captured here when the worktree is detached)";
    return "(none — the worker wrote no gitignored deliverables; this only covers files git ignores, not code changes)";
  }

  function renderResultSection() {
    const wrap = el("div", {
      class: "card-detail-section result-section",
      id: "detail-result",
    });
    wrap.appendChild(el("h3", {}, "Result"));
    if (detailResultState === "loading" || detailResultState === "idle") {
      wrap.appendChild(el("p", { class: "hint" }, "Loading…"));
      return wrap;
    }
    if (detailResultState === "error") {
      wrap.appendChild(
        el(
          "p",
          { class: "hint artifacts-error" },
          `Failed to load result: ${detailResultError}`,
        ),
      );
      return wrap;
    }
    const r = detailResult || {};
    const wt = r.worktree || {};
    const dl = el("dl", { class: "result-dl" });
    const row = (k, v) => {
      if (v === null || v === undefined || v === "") return;
      dl.appendChild(el("dt", {}, k));
      dl.appendChild(el("dd", {}, v));
    };
    row("status", el("span", { class: "badge" }, r.status || "–"));
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
          }, `${artCount} snapshot${artCount === 1 ? "" : "s"} →`)
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
            `${traceCount} retained →`,
          )
        : el("span", { class: "hint" }, "none"),
    );
    wrap.appendChild(dl);
    // Next steps: copyable command lines (never one-click for merge/prune).
    const steps = r.next_steps || [];
    if (steps.length) {
      const stepWrap = el("div", { class: "result-next" }, [
        el("div", { class: "result-next-label" }, "Next steps"),
      ]);
      const ul = el("ul", { class: "next-steps" });
      for (const s of steps) {
        const isComment = s.trim().startsWith("(");
        if (isComment) {
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
      wrap.appendChild(stepWrap);
    }
    return wrap;
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return "?";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MiB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  }

  function renderArtifactsSection(cardId) {
    const wrap = el("div", {
      class: "card-detail-section",
      id: "detail-artifacts",
    }, [el("h3", {}, "Artifacts")]);
    if (detailArtifactsState === "loading") {
      wrap.appendChild(el("p", { class: "hint" }, "Loading…"));
      return wrap;
    }
    if (detailArtifactsState === "error") {
      wrap.appendChild(
        el(
          "p",
          { class: "hint artifacts-error" },
          `Failed to load: ${detailArtifactsError}`,
        ),
      );
      return wrap;
    }
    if (
      detailArtifactsState !== "loaded" ||
      !detailArtifacts ||
      !detailArtifacts.snapshots ||
      detailArtifacts.snapshots.length === 0
    ) {
      wrap.appendChild(
        el(
          "p",
          { class: "hint" },
          // Generic message until the result payload lands; then the
          // worktree-state-aware explanation from artifactsEmptyHint.
          detailResultState === "loaded"
            ? artifactsEmptyHint(detailResult)
            : "(none — gitignored worker output is captured here when a worktree is detached)",
        ),
      );
      return wrap;
    }
    for (const snap of detailArtifacts.snapshots) {
      const fileCountLabel =
        snap.truncated && snap.total_file_count
          ? `${snap.file_count} of ${snap.total_file_count} files`
          : `${snap.file_count} file${snap.file_count === 1 ? "" : "s"}`;
      const summary = el(
        "summary",
        {},
        `${snap.snapshot} · ${fileCountLabel} · ${fmtBytes(snap.total_bytes)}`,
      );
      const list = el("ul", { class: "artifact-files" });
      for (const f of snap.files) {
        const href = `/api/cards/${encodeURIComponent(cardId)}/artifacts/${encodeURIComponent(snap.snapshot)}/file?path=${encodeURIComponent(f.path)}`;
        list.appendChild(
          el("li", {}, [
            el(
              "a",
              { href, target: "_blank", rel: "noopener" },
              f.path,
            ),
            el("span", { class: "artifact-size" }, fmtBytes(f.size)),
          ]),
        );
      }
      if (snap.truncated) {
        list.appendChild(
          el(
            "li",
            { class: "artifact-truncated" },
            `(${snap.total_file_count - snap.file_count} more files not shown — copy from disk)`,
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

  function renderTranscriptsSection(cardId) {
    const wrap = el(
      "div",
      { class: "card-detail-section", id: "detail-transcripts" },
      [el("h3", {}, "Transcripts")],
    );
    if (detailTracesState === "loading" || detailTracesState === "idle") {
      wrap.appendChild(el("p", { class: "hint" }, "Loading…"));
      return wrap;
    }
    if (detailTracesState === "error") {
      wrap.appendChild(
        el(
          "p",
          { class: "hint artifacts-error" },
          `Failed to load: ${detailTracesError}`,
        ),
      );
      return wrap;
    }
    const traces = (detailTraces && detailTraces.traces) || [];
    if (!traces.length) {
      wrap.appendChild(
        el(
          "p",
          { class: "hint" },
          "(none — full agent transcripts are saved here when a worker runs; the most recent few per role are kept)",
        ),
      );
      return wrap;
    }
    const list = el("ul", { class: "trace-files" });
    traces.forEach((t, i) => {
      const href = `/api/cards/${encodeURIComponent(cardId)}/traces/${encodeURIComponent(t.trace_id)}/file`;
      list.appendChild(
        el("li", {}, [
          el("a", { href, target: "_blank", rel: "noopener" }, t.trace_id),
          el(
            "span",
            { class: "trace-meta" },
            `${t.role || "?"} · ${fmtTime(t.at)} · ${fmtBytes(t.size)}`,
          ),
          i === 0 ? el("span", { class: "trace-latest" }, "latest") : null,
        ]),
      );
    });
    wrap.appendChild(list);
    return wrap;
  }

  async function loadTraces(id) {
    detailTracesState = "loading";
    detailTracesError = "";
    detailTraces = null;
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
    try {
      const data = await fetchJSON(
        `/api/cards/${encodeURIComponent(id)}/traces`,
      );
      if (selectedCardId !== id) return;
      detailTraces = data;
      detailTracesState = "loaded";
    } catch (err) {
      if (selectedCardId !== id) return;
      detailTracesState = "error";
      detailTracesError = err.message;
    }
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
  }

  async function loadArtifacts(id) {
    detailArtifactsState = "loading";
    detailArtifactsError = "";
    detailArtifacts = null;
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
    try {
      const data = await fetchJSON(
        `/api/cards/${encodeURIComponent(id)}/artifacts`,
      );
      if (selectedCardId !== id) return;
      detailArtifacts = data;
      detailArtifactsState = "loaded";
      // Default: expand the newest snapshot once after the listing
      // lands. After that expandedSnapshots is owned by the user's
      // toggle events.
      const snaps = data && data.snapshots;
      if (snaps && snaps.length) expandedSnapshots.add(snaps[0].snapshot);
    } catch (err) {
      if (selectedCardId !== id) return;
      detailArtifactsState = "error";
      detailArtifactsError = err.message;
    }
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
  }

  async function loadResult(id) {
    detailResultState = "loading";
    detailResultError = "";
    detailResult = null;
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
    try {
      const data = await fetchJSON(`/api/cards/${encodeURIComponent(id)}/result`);
      if (selectedCardId !== id) return;
      detailResult = data;
      detailResultState = "loaded";
    } catch (err) {
      if (selectedCardId !== id) return;
      detailResultState = "error";
      detailResultError = err.message;
    }
    if (selectedCardId === id && detailLastCard) {
      renderDetailInto(detailLastCard);
    }
  }

  async function fetchJSON(url) {
    const r = await fetch(url, {
      headers: { accept: "application/json" },
      cache: "no-store",
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  async function refreshDetail() {
    if (!selectedCardId || !detailModal) return;
    try {
      const card = await fetchJSON(
        `/api/cards/${encodeURIComponent(selectedCardId)}`,
      );
      // Guard against late responses arriving after the user closed the
      // modal or navigated to a different card.
      if (selectedCardId !== card.id) return;
      detailLastCard = card;
      renderDetailInto(card);
    } catch (err) {
      if (!detailModalBody) return;
      detailModalBody.replaceChildren(
        el(
          "p",
          { class: "card-detail-loading" },
          `Failed to load: ${err.message}`,
        ),
      );
    }
  }

  function openDetailModal(id) {
    selectedCardId = id;
    detailLastCard = null;
    detailArtifacts = null;
    detailArtifactsState = "loading";
    detailArtifactsError = "";
    detailResult = null;
    detailResultState = "loading";
    detailResultError = "";
    detailTraces = null;
    detailTracesState = "loading";
    detailTracesError = "";
    expandedSnapshots.clear();
    const modal = ensureDetailModal();
    modal.classList.remove("hidden");
    if (!detailKeydownHandler) {
      detailKeydownHandler = (ev) => {
        if (ev.key === "Escape") closeDetailModal();
      };
      document.addEventListener("keydown", detailKeydownHandler);
    }
    // Show "Loading…" until the per-card fetch lands.
    renderDetailInto(null);
    refreshDetail();
    loadResult(id);
    loadArtifacts(id);
    loadTraces(id);
  }

  function closeDetailModal() {
    selectedCardId = null;
    detailLastCard = null;
    detailArtifacts = null;
    detailArtifactsState = "idle";
    detailArtifactsError = "";
    detailResult = null;
    detailResultState = "idle";
    detailResultError = "";
    detailTraces = null;
    detailTracesState = "idle";
    detailTracesError = "";
    if (detailModal) detailModal.classList.add("hidden");
    if (detailKeydownHandler) {
      document.removeEventListener("keydown", detailKeydownHandler);
      detailKeydownHandler = null;
    }
    for (const node of document.querySelectorAll(".card.selected")) {
      node.classList.remove("selected");
    }
  }

  function selectCard(id) {
    for (const node of document.querySelectorAll(".card.selected")) {
      node.classList.remove("selected");
    }
    const active = document.querySelector(
      `.card[data-card-id="${CSS.escape(id)}"]`,
    );
    if (active) active.classList.add("selected");
    openDetailModal(id);
  }

  async function tick() {
    setStatus("live");
    try {
      const data = await fetchJSON("/api/board");
      renderBoard(data);
      renderEvents(data.recent_events || []);
      renderRuntime(data.runtime || {}, data.daemon || null);
      if (selectedCardId) refreshDetail();
      updatedAtEl.textContent = fmtTime(data.generated_at);
    } catch (err) {
      setStatus("error");
      updatedAtEl.textContent = `error: ${err.message}`;
      return;
    } finally {
      setTimeout(() => setStatus("idle"), 400);
    }
  }

  tick();
  setInterval(tick, pollMs);
})();

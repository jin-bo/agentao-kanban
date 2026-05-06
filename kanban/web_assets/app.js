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
    addCardModal = backdrop;
    document.body.appendChild(addCardModal);
    return addCardModal;
  }

  function openAddCardModal() {
    const modal = ensureAddCardModal();
    modal.classList.remove("hidden");
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

    const sections = [];

    if (card.goal) {
      sections.push(
        el("div", { class: "card-detail-section" }, [
          el("h3", {}, "Goal"),
          el("p", { class: "hint" }, card.goal),
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

    detailModalBody.replaceChildren(head, ...sections);
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
  }

  function closeDetailModal() {
    selectedCardId = null;
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

(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const { el, fmtRelTime, fmtTime, shortId } = ns;

  function createBoardView(options) {
    const addCardModal = options.addCardModal;
    const onSelectCard = options.onSelectCard;
    const eventsEl = document.getElementById("events");
    const runtimeEl = document.getElementById("runtime");
    const boardDirEl = document.getElementById("board-dir");

    function renderBoard(data, selectedCardId) {
      boardDirEl.textContent = data.board_dir || "";
      const writesEnabled = !!data.writes_enabled;
      addCardModal.setCards(
        (data.columns || [])
          .filter((c) => c.status !== "done")
          .flatMap((c) => c.cards || []),
      );
      for (const col of data.columns) {
        const slot = document.getElementById(`col-${col.status}`);
        if (!slot) continue;
        const head = el("div", { class: "column-head" }, [
          col.title,
          el("span", { class: "count" }, String(col.count)),
        ]);
        const body = el("div", { class: "column-body" });
        for (const card of col.cards) body.appendChild(renderCard(card, selectedCardId));
        if (col.status === "inbox" && writesEnabled) {
          slot.replaceChildren(head, addCardModal.makeTrigger(), body);
        } else {
          slot.replaceChildren(head, body);
        }
      }
    }

    function renderCard(card, selectedCardId) {
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
      return el(
        "div",
        {
          class: "card" + (card.id === selectedCardId ? " selected" : ""),
          dataset: { cardId: card.id },
          onclick: () => onSelectCard(card.id),
        },
        children,
      );
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
                        (c.worker_id ? ` -> ${c.worker_id}` : " (unassigned)"),
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

    function markSelected(id) {
      for (const node of document.querySelectorAll(".card.selected")) {
        node.classList.remove("selected");
      }
      const active = document.querySelector(
        `.card[data-card-id="${CSS.escape(id)}"]`,
      );
      if (active) active.classList.add("selected");
    }

    function clearSelected() {
      for (const node of document.querySelectorAll(".card.selected")) {
        node.classList.remove("selected");
      }
    }

    return {
      clearSelected,
      markSelected,
      renderBoard,
      renderEvents,
      renderRuntime,
    };
  }

  ns.createBoardView = createBoardView;
})();

(function () {
  "use strict";

  const config = window.KANBAN_CONFIG || { pollIntervalMs: 5000 };
  const pollMs = Math.max(Number(config.pollIntervalMs) || 5000, 250);

  const eventsEl = document.getElementById("events");
  const runtimeEl = document.getElementById("runtime");
  const detailEl = document.getElementById("detail-panel");
  const statusDot = document.getElementById("status-dot");
  const updatedAtEl = document.getElementById("updated-at");
  const boardDirEl = document.getElementById("board-dir");

  let selectedCardId = null;
  let selectedCardSig = null;

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
    for (const col of data.columns) {
      const slot = document.getElementById(`col-${col.status}`);
      if (!slot) continue;
      const head = el("div", { class: "column-head" }, [
        col.title,
        el("span", { class: "count" }, String(col.count)),
      ]);
      const body = el("div", { class: "column-body" });
      for (const card of col.cards) body.appendChild(renderCard(card));
      slot.replaceChildren(head, body);
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

  function renderRuntime(runtime) {
    const blocks = [];
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

  function renderDetail(card) {
    if (!card) {
      detailEl.replaceChildren(
        el("h2", {}, "Card detail"),
        el("p", { class: "hint" }, "Click any card to inspect."),
      );
      return;
    }
    const dl = el("dl", {});
    const append = (k, v) => {
      if (v === null || v === undefined || v === "") return;
      dl.appendChild(el("dt", {}, k));
      dl.appendChild(
        el("dd", {}, Array.isArray(v) ? v.join(", ") || "–" : String(v)),
      );
    };
    append("id", card.id);
    append("status", card.status);
    append("priority", card.priority);
    append("owner", card.owner_role);
    append("profile", card.agent_profile);
    append("rework", card.rework_iteration);
    append("depends_on", card.depends_on);
    append("updated_at", card.updated_at);
    append("created_at", card.created_at);

    const children = [
      el("div", { class: "detail" }, [
        el("h3", {}, card.title || "(untitled)"),
        card.goal ? el("p", { class: "hint" }, card.goal) : null,
        dl,
      ]),
    ];
    if (card.blocked_reason) {
      children.push(el("div", { class: "blocked-reason" }, card.blocked_reason));
    }
    if (card.acceptance_criteria && card.acceptance_criteria.length) {
      children.push(
        el(
          "pre",
          {},
          card.acceptance_criteria.map((c, i) => `${i + 1}. ${c}`).join("\n"),
        ),
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
              {
                class:
                  "event-tag" + (e.role ? ` role-${e.role}` : ""),
              },
              e.display_tag || "info",
            ),
            el("span", { class: "event-message" }, e.message || ""),
          ]),
        );
      }
      children.push(el("h2", {}, "Card events"), ev);
    }
    detailEl.replaceChildren(el("h2", {}, "Card detail"), ...children);
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
    if (!selectedCardId) {
      renderDetail(null);
      return;
    }
    try {
      const card = await fetchJSON(
        `/api/cards/${encodeURIComponent(selectedCardId)}`,
      );
      renderDetail(card);
    } catch (err) {
      detailEl.replaceChildren(
        el("h2", {}, "Card detail"),
        el("p", { class: "hint" }, `Failed to load: ${err.message}`),
      );
    }
  }

  function selectCard(id) {
    selectedCardId = id;
    for (const node of document.querySelectorAll(".card.selected")) {
      node.classList.remove("selected");
    }
    const active = document.querySelector(
      `.card[data-card-id="${CSS.escape(id)}"]`,
    );
    if (active) active.classList.add("selected");
    refreshDetail();
  }

  async function tick() {
    setStatus("live");
    try {
      const data = await fetchJSON("/api/board");
      renderBoard(data);
      renderEvents(data.recent_events || []);
      renderRuntime(data.runtime || {});
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

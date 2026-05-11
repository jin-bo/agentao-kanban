(function () {
  "use strict";

  const ns = (window.KanbanWeb = window.KanbanWeb || {});
  const { el, shortId } = ns;

  function createAddCardModal(options) {
    const onCreated = options && options.onCreated ? options.onCreated : () => {};
    let dependencyCards = [];
    let modal = null;
    let keydownHandler = null;

    function makeTrigger() {
      return el(
        "button",
        {
          type: "button",
          class: "add-toggle",
          onclick: open,
          "aria-label": "Add card",
        },
        "+ Add card",
      );
    }

    function setCards(cards) {
      dependencyCards = cards || [];
      // Only touch the DOM while the modal is actually visible — the
      // board poll calls this every few seconds and the datalist is
      // rebuilt from scratch when open() runs anyway.
      if (modal && !modal.classList.contains("hidden") && modal._refreshDeps) {
        modal._refreshDeps();
      }
    }

    function ensure() {
      if (modal) return modal;
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
        placeholder: "Goal - what does done look like?",
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
        const opts = dependencyCards
          .filter((c) => !selectedDeps.has(c.id))
          .map((c) =>
            el(
              "option",
              { value: c.id },
              `${shortId(c.id)} - ${c.title || "(untitled)"} [${c.status}]`,
            ),
          );
        dependsDatalist.replaceChildren(...opts);
      }

      function renderDepsChips() {
        const chips = Array.from(selectedDeps).map((id) => {
          const meta = dependencyCards.find((c) => c.id === id);
          const label = meta
            ? `${shortId(id)} - ${meta.title || "(untitled)"}`
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
              "x",
            ),
          ]);
        });
        dependsListEl.replaceChildren(...chips);
      }

      function tryAddDep() {
        const raw = dependsInput.value.trim();
        if (!raw) return;
        let match = dependencyCards.find((c) => c.id === raw);
        if (!match) {
          const prefixed = dependencyCards.filter((c) => c.id.startsWith(raw));
          if (prefixed.length === 1) match = prefixed[0];
        }
        if (!match) {
          errorEl.textContent = `No card matches "${raw}" - pick from the autocomplete list, or paste a full id from \`kanban list\`.`;
          return;
        }
        if (selectedDeps.has(match.id)) {
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
          ev.preventDefault();
          tryAddDep();
        }
      });
      dependsInput.addEventListener("change", () => {
        const v = dependsInput.value.trim();
        if (v && dependencyCards.some((c) => c.id === v)) {
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
      cardForm.addEventListener("click", (ev) => ev.stopPropagation());

      const backdrop = el(
        "div",
        {
          class: "modal-backdrop hidden",
          role: "dialog",
          "aria-modal": "true",
          "aria-label": "Add card",
          onclick: () => close(),
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

      cancelBtn.addEventListener("click", () => close());

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
        submitBtn.textContent = "Creating...";
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
          close();
          onCreated();
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
      modal = backdrop;
      document.body.appendChild(modal);
      return modal;
    }

    function open() {
      const active = ensure();
      active.classList.remove("hidden");
      if (active._refreshDeps) active._refreshDeps();
      setTimeout(() => active._titleInput.focus(), 0);
      if (!keydownHandler) {
        keydownHandler = (ev) => {
          if (ev.key === "Escape") close();
        };
        document.addEventListener("keydown", keydownHandler);
      }
    }

    function close() {
      if (!modal) return;
      modal.classList.add("hidden");
      modal._reset();
      if (keydownHandler) {
        document.removeEventListener("keydown", keydownHandler);
        keydownHandler = null;
      }
    }

    return {
      makeTrigger,
      setCards,
    };
  }

  ns.createAddCardModal = createAddCardModal;
})();

(function () {
  "use strict";

  const config = window.KANBAN_CONFIG || { pollIntervalMs: 5000 };
  const pollMs = Math.max(Number(config.pollIntervalMs) || 5000, 250);
  const {
    fetchJSON,
    fmtTime,
  } = window.KanbanWeb;

  const statusDot = document.getElementById("status-dot");
  const updatedAtEl = document.getElementById("updated-at");

  const addCardModal = window.KanbanWeb.createAddCardModal({
    onCreated: () => tick(),
  });
  const detailModal = window.KanbanWeb.createDetailModal({
    onClose: () => boardView.clearSelected(),
    onMutated: () => tick(),
  });
  const boardView = window.KanbanWeb.createBoardView({
    addCardModal,
    onSelectCard: selectCard,
  });

  function setStatus(kind) {
    statusDot.classList.remove("idle", "live", "error");
    statusDot.classList.add(kind);
  }

  function selectCard(id) {
    boardView.markSelected(id);
    detailModal.open(id);
  }

  async function tick() {
    setStatus("live");
    try {
      const data = await fetchJSON("/api/board");
      boardView.renderBoard(data, detailModal.selectedId());
      boardView.renderEvents(data.recent_events || []);
      boardView.renderRuntime(data.runtime || {}, data.daemon || null);
      detailModal.setWritesEnabled(!!data.writes_enabled);
      detailModal.setClaimedCardIds(
        ((data.runtime && data.runtime.claims) || []).map((c) => c.card_id),
      );
      if (detailModal.selectedId()) detailModal.refresh();
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

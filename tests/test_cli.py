from __future__ import annotations

from pathlib import Path

import pytest

from kanban.cli import main
from kanban.executors import MockAgentaoExecutor
from kanban.models import CardPriority, CardStatus
from kanban.orchestrator import KanbanOrchestrator
from kanban.store_markdown import MarkdownBoardStore


def _add_card(board: Path, title: str = "T", goal: str = "G") -> str:
    rc = main(["--board", str(board), "card", "add", "--title", title, "--goal", goal])
    assert rc == 0
    store = MarkdownBoardStore(board)
    cards = store.list_cards()
    assert len(cards) == 1
    return cards[0].id


class TestCardEdit:
    def test_edits_title_goal_priority(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--title", "New Title", "--goal", "New Goal", "--priority", "HIGH",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.title == "New Title"
        assert card.goal == "New Goal"
        assert card.priority == CardPriority.HIGH
        assert any("Manual edit via CLI" in h for h in card.history)

    def test_set_status_to_done(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main(["--board", str(board), "card", "edit", cid, "--set-status", "done"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.DONE
        assert card.owner_role is None
        assert any("Status manually set to done" in h for h in card.history)

    def test_set_status_rejects_doing(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        with pytest.raises(SystemExit):
            main(["--board", str(board), "card", "edit", cid, "--set-status", "doing"])

    def test_set_status_rejects_review(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        with pytest.raises(SystemExit):
            main(["--board", str(board), "card", "edit", cid, "--set-status", "review"])

    def test_set_status_blocked_requires_reason(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main(["--board", str(board), "card", "edit", cid, "--set-status", "blocked"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--blocked-reason" in err

    def test_set_status_blocked_with_reason(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "waiting on input",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.BLOCKED
        assert card.blocked_reason == "waiting on input"

    def test_clear_blocked_reason(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "R",
        ])
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--clear-blocked-reason",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.blocked_reason is None
        assert any("Blocked reason cleared via CLI" in h for h in card.history)

    def test_blocked_reason_and_clear_mutually_exclusive(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        with pytest.raises(SystemExit):
            main([
                "--board", str(board), "card", "edit", cid,
                "--blocked-reason", "r", "--clear-blocked-reason",
            ])

    def test_noop_edit_errors(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main(["--board", str(board), "card", "edit", cid])
        assert rc == 2
        assert "Nothing to edit" in capsys.readouterr().err

    def test_unknown_card_errors(self, tmp_path: Path):
        board = tmp_path / "board"
        _add_card(board)
        rc = main(["--board", str(board), "card", "edit", "no-such-id", "--title", "x"])
        assert rc == 1


class TestCardContext:
    def test_add_and_list(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "context", "add", cid,
            "--path", "docs/api.md", "--kind", "required", "--note", "contract",
        ])
        assert rc == 0
        rc = main(["--board", str(board), "card", "context", "list", cid])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[required] docs/api.md" in out
        assert "contract" in out

    def test_add_upserts_same_path(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "context", "add", cid,
            "--path", "x.md", "--kind", "optional",
        ])
        main([
            "--board", str(board), "card", "context", "add", cid,
            "--path", "x.md", "--kind", "required", "--note", "now required",
        ])
        card = MarkdownBoardStore(board).get_card(cid)
        assert len(card.context_refs) == 1
        assert card.context_refs[0].kind == "required"
        assert card.context_refs[0].note == "now required"
        assert sum("Context updated" in h for h in card.history) == 1
        assert sum("Context added" in h for h in card.history) == 1

    def test_rejects_invalid_kind(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        with pytest.raises(SystemExit):
            main([
                "--board", str(board), "card", "context", "add", cid,
                "--path", "x.md", "--kind", "mandatory",
            ])

    def test_remove_by_path(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "context", "add", cid, "--path", "a.md"])
        main(["--board", str(board), "card", "context", "add", cid, "--path", "b.md"])
        rc = main(["--board", str(board), "card", "context", "rm", cid, "--path", "a.md"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert [r.path for r in card.context_refs] == ["b.md"]
        assert any("Context removed: a.md" in h for h in card.history)

    def test_remove_missing_path_errors(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main(["--board", str(board), "card", "context", "rm", cid, "--path", "nope.md"])
        assert rc == 1

    def test_list_preserves_insertion_order_across_reload(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        for p in ("a.md", "b.md", "c.md"):
            main(["--board", str(board), "card", "context", "add", cid, "--path", p])
        main(["--board", str(board), "card", "context", "list", cid])
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.startswith("[")]
        assert [l.split()[1] for l in lines] == ["a.md", "b.md", "c.md"]


class TestCardAcceptance:
    def test_add_then_list(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "First"])
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "Second"])
        rc = main(["--board", str(board), "card", "acceptance", "list", cid])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1. First" in out
        assert "2. Second" in out

    def test_rm_by_index(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "A"])
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "B"])
        rc = main(["--board", str(board), "card", "acceptance", "rm", cid, "--index", "1"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.acceptance_criteria == ["B"]
        assert any("removed at index 1" in h for h in card.history)

    def test_rm_invalid_index(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "A"])
        rc = main(["--board", str(board), "card", "acceptance", "rm", cid, "--index", "5"])
        assert rc == 2
        assert "Invalid index" in capsys.readouterr().err

    def test_clear(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "X"])
        rc = main(["--board", str(board), "card", "acceptance", "clear", cid])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.acceptance_criteria == []
        assert any("Acceptance criteria cleared via CLI" in h for h in card.history)


class TestRequeue:
    def test_blocked_to_inbox_default(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "needs context",
        ])
        rc = main(["--board", str(board), "requeue", cid])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.INBOX
        assert card.blocked_reason is None
        assert any("Requeued from blocked to inbox" in h for h in card.history)

    def test_to_ready_with_note(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "R",
        ])
        rc = main([
            "--board", str(board), "requeue", cid,
            "--to", "ready", "--note", "added dataset",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.READY
        assert card.blocked_reason is None
        assert card.owner_role is None
        assert any(
            "Requeued from blocked to ready: added dataset" in h for h in card.history
        )

    def test_non_blocked_is_allowed_and_logs_previous(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)  # starts in inbox
        # Move to ready via the native dispatcher state; use move subcommand.
        main(["--board", str(board), "move", cid, "ready"])
        rc = main(["--board", str(board), "requeue", cid, "--to", "inbox"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.INBOX
        assert any("Requeued from ready to inbox" in h for h in card.history)

    def test_unknown_card(self, tmp_path: Path):
        board = tmp_path / "board"
        _add_card(board)
        rc = main(["--board", str(board), "requeue", "no-such"])
        assert rc == 1


def _seed_events(board: Path) -> str:
    """Create one card and run it through the mock executor so events.log has
    a mix of plain and execution records."""
    rc = main(["--board", str(board), "card", "add", "--title", "E", "--goal", "g"])
    assert rc == 0
    store = MarkdownBoardStore(board)
    cid = store.list_cards()[0].id
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    orch.run_until_idle(max_steps=20)
    return cid


class TestEvents:
    def test_lists_all_mixed(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[planner]" in out
        assert "[system]" in out

    def test_filter_by_card(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events", cid])
        assert rc == 0
        out = capsys.readouterr().out
        for line in out.splitlines():
            assert cid[:8] in line

    def test_role_filter_excludes_plain_events(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events", cid, "--role", "worker"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[worker]" in out
        assert "[system]" not in out
        assert "[planner]" not in out

    def test_limit(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        _seed_events(board)
        capsys.readouterr()  # drain seed output
        rc = main(["--board", str(board), "events", "--limit", "3"])
        assert rc == 0
        out = capsys.readouterr().out
        assert len([l for l in out.splitlines() if l]) == 3

    def test_json_output_parses(self, tmp_path: Path, capsys):
        import json as _j
        board = tmp_path / "board"
        _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events", "--json", "--limit", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        for line in out.splitlines():
            record = _j.loads(line)
            assert "at" in record
            assert "card_id" in record
            assert "message" in record

    def test_no_events_message(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        board.mkdir()
        (board / "cards").mkdir()
        rc = main(["--board", str(board), "events"])
        assert rc == 0
        assert "(no events)" in capsys.readouterr().out


class TestTraces:
    def _seed_with_traces(self, tmp_path: Path) -> str:
        """Create a board whose mock-run cards have fake raw transcripts."""
        from datetime import datetime, timedelta, timezone

        board = tmp_path / "board"
        board.mkdir()
        (board / "cards").mkdir()
        cid = _add_card(board)
        raw_dir = tmp_path / "raw" / cid
        raw_dir.mkdir(parents=True)
        base = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        # two worker traces, one planner trace, one reviewer trace
        for role, offset in [
            ("planner", 0),
            ("worker", 5),
            ("worker", 10),
            ("reviewer", 15),
        ]:
            stamp = (base + timedelta(minutes=offset)).strftime("%Y%m%dT%H%M%S%fZ")
            (raw_dir / f"{role}-{stamp}.md").write_text(f"{role} transcript\n")
        return cid

    def test_list_all(self, tmp_path: Path, capsys):
        cid = self._seed_with_traces(tmp_path)
        capsys.readouterr()
        rc = main(["--board", str(tmp_path / "board"), "traces", cid])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l]
        assert len(lines) == 4
        assert any("[planner]" in l for l in lines)
        assert sum(1 for l in lines if "[worker]" in l) == 2

    def test_filter_by_role(self, tmp_path: Path, capsys):
        cid = self._seed_with_traces(tmp_path)
        capsys.readouterr()
        rc = main(["--board", str(tmp_path / "board"), "traces", cid, "--role", "worker"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l]
        assert len(lines) == 2
        assert all("[worker]" in l for l in lines)

    def test_latest(self, tmp_path: Path, capsys):
        cid = self._seed_with_traces(tmp_path)
        capsys.readouterr()
        rc = main(["--board", str(tmp_path / "board"), "traces", cid, "--latest"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l]
        assert len(lines) == 1
        assert "[reviewer]" in lines[0]  # reviewer is at offset=15, the latest

    def test_no_traces_dir_is_friendly(self, tmp_path: Path, capsys):
        # Card exists but no raw dir was ever created.
        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "traces", cid])
        assert rc == 0
        assert "no traces retained" in capsys.readouterr().out

    def test_unknown_card(self, tmp_path: Path):
        board = tmp_path / "board"
        _add_card(board)
        rc = main(["--board", str(board), "traces", "no-such"])
        assert rc == 1


class TestDoctor:
    def test_healthy_board_exit_zero(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        _add_card(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "doctor"])
        assert rc == 0
        assert "healthy" in capsys.readouterr().out

    def test_missing_dependency_errors(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        # Hand-edit the card file to add a fake dependency.
        card_file = board / "cards" / f"{cid}.md"
        content = card_file.read_text()
        content = content.replace("depends_on = []", 'depends_on = ["ghost-id"]')
        card_file.write_text(content)

        rc = main(["--board", str(board), "doctor"])
        assert rc == 2

    def test_done_without_verification_warns(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "edit", cid, "--set-status", "done"])
        rc = main(["--board", str(board), "doctor"])
        assert rc == 1  # warning only

    def test_done_with_legacy_string_verification_is_ok(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        store = MarkdownBoardStore(board)
        store.update_card(cid, outputs={"implementation": "i", "review": "r", "verification": "Acceptance criteria verified."})
        main(["--board", str(board), "card", "edit", cid, "--set-status", "done"])
        rc = main(["--board", str(board), "doctor"])
        assert rc == 0  # legacy string is accepted

    def test_review_without_implementation_errors(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        # Hand-edit status to review without populating outputs.
        card_file = board / "cards" / f"{cid}.md"
        text = card_file.read_text().replace('status = "inbox"', 'status = "review"')
        card_file.write_text(text)
        rc = main(["--board", str(board), "doctor"])
        assert rc == 2

    def test_invalid_context_kind_warns(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        # Hand-edit to inject an invalid kind that survives load.
        card_file = board / "cards" / f"{cid}.md"
        text = card_file.read_text().replace(
            "context_refs = []",
            'context_refs = [{ path = "x.md", kind = "mandatory", note = "" }]',
        )
        card_file.write_text(text)
        rc = main(["--board", str(board), "doctor"])
        assert rc == 1

    def test_json_output_schema(self, tmp_path: Path, capsys):
        import json as _j

        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "edit", cid, "--set-status", "done"])
        capsys.readouterr()
        rc = main(["--board", str(board), "doctor", "--json"])
        assert rc == 1
        payload = _j.loads(capsys.readouterr().out)
        assert "checks" in payload
        for c in payload["checks"]:
            assert set(c.keys()) >= {"severity", "rule", "card_id", "message"}
            assert c["severity"] in ("error", "warning")

    def test_unparseable_card_reported(self, tmp_path: Path):
        board = tmp_path / "board"
        _add_card(board)
        # Drop a broken card file alongside.
        (board / "cards" / "broken.md").write_text("+++\ngarbage\n+++\n")
        rc = main(["--board", str(board), "doctor"])
        assert rc == 2

    def test_valid_toml_missing_required_field_is_unparseable(self, tmp_path: Path):
        # Regression: Card(**kwargs) raises TypeError when a required field
        # like `goal` is absent. That must not kill board load — it should
        # be captured as `unparseable-card` like any other malformed file.
        board = tmp_path / "board"
        _add_card(board)
        (board / "cards" / "no-goal.md").write_text(
            '+++\n'
            'id = "no-goal"\n'
            'title = "T"\n'
            'status = "inbox"\n'
            'priority = 2\n'
            # `goal` intentionally missing — Card.__init__ requires it
            'acceptance_criteria = []\n'
            'context_refs = []\n'
            'depends_on = []\n'
            'history = []\n'
            'created_at = 2026-01-01T00:00:00+00:00\n'
            'updated_at = 2026-01-01T00:00:00+00:00\n'
            '+++\n',
            encoding="utf-8",
        )
        rc = main(["--board", str(board), "doctor"])
        assert rc == 2
        # And the board still lists the healthy card.
        cards = MarkdownBoardStore(board).list_cards()
        assert len(cards) == 1
        assert cards[0].id != "no-goal"


class TestSetStatusFromBlocked:
    """Codex review P2b: `--set-status` leaving BLOCKED must clear the stale reason."""

    def test_set_status_to_inbox_clears_blocked_reason(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "R",
        ])
        # Now force back to inbox without explicit --clear-blocked-reason.
        rc = main(["--board", str(board), "card", "edit", cid, "--set-status", "inbox"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.INBOX
        assert card.blocked_reason is None

    def test_set_status_to_done_clears_blocked_reason(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "R",
        ])
        rc = main(["--board", str(board), "card", "edit", cid, "--set-status", "done"])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.DONE
        assert card.blocked_reason is None

    def test_rejects_blocked_reason_with_non_blocked_status(self, tmp_path: Path, capsys):
        # Codex P2 round 5: the combination leaves contradictory state
        # (non-blocked status + live reason). Reject rather than paper over.
        board = tmp_path / "board"
        cid = _add_card(board)
        for target in ("inbox", "ready", "done"):
            rc = main([
                "--board", str(board), "card", "edit", cid,
                "--set-status", target, "--blocked-reason", "R",
            ])
            assert rc == 2, f"status {target} should be rejected"
        err = capsys.readouterr().err
        assert "only valid when the card is or is being moved to blocked" in err

    def test_rejects_blocked_reason_on_non_blocked_card_without_status_change(
        self, tmp_path: Path, capsys
    ):
        # Codex P2 round 6: --blocked-reason alone (no --set-status) on a
        # non-blocked card would write a live reason on an inbox/ready/done
        # card. That's the same contradictory state as the combined case.
        board = tmp_path / "board"
        cid = _add_card(board)  # starts INBOX
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--blocked-reason", "phantom",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "only valid when the card is or is being moved to blocked" in err
        # Card must remain clean — no reason got persisted.
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.blocked_reason is None

    def test_updating_reason_on_already_blocked_card_is_allowed(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "first",
        ])
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--blocked-reason", "updated",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.status == CardStatus.BLOCKED
        assert card.blocked_reason == "updated"

    def test_set_status_blocked_keeps_reason(self, tmp_path: Path):
        # Regression guard: the clear must NOT fire when new status is
        # itself BLOCKED; the operator just provided the reason.
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "still blocked",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.blocked_reason == "still blocked"


class TestContextRefKindValidation:
    """Codex review P2a: invalid kind must fail fast on write paths."""

    def test_direct_construction_rejects_bad_kind(self):
        from kanban.models import ContextRef

        with pytest.raises(ValueError):
            ContextRef(path="x.md", kind="mandatory")

    def test_card_constructor_rejects_bad_kind_dict(self):
        from kanban.models import Card, ContextRef

        with pytest.raises(ValueError):
            Card(title="T", goal="G", context_refs=[{"path": "x.md", "kind": "mandatory"}])

    def test_update_card_rejects_bad_kind(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        store = MarkdownBoardStore(board)
        with pytest.raises(ValueError):
            store.update_card(cid, context_refs=[{"path": "x.md", "kind": "mandatory"}])

    def test_load_preserves_bad_kind_for_doctor(self, tmp_path: Path):
        # Load path stays lenient so `doctor` can see the problem.
        from kanban.models import ContextRef

        board = tmp_path / "board"
        cid = _add_card(board)
        card_file = board / "cards" / f"{cid}.md"
        text = card_file.read_text().replace(
            "context_refs = []",
            'context_refs = [{ path = "x.md", kind = "mandatory", note = "" }]',
        )
        card_file.write_text(text)

        reloaded = MarkdownBoardStore(board).get_card(cid)
        assert len(reloaded.context_refs) == 1
        assert reloaded.context_refs[0].kind == "mandatory"  # kept for doctor
        # doctor flags as warning
        rc = main(["--board", str(board), "doctor"])
        assert rc == 1


class TestCliEditsEmitEvents:
    """CLI writes must show up in `kanban events` (Codex review P2)."""

    def test_title_edit_is_eventful(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "edit", cid, "--title", "New"])
        events = MarkdownBoardStore(board).events_for_card(cid)
        assert any(e.message == "Manual edit via CLI" for e in events)

    def test_blocked_reason_edit_is_eventful(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        # Block first, then update reason on the now-blocked card.
        main([
            "--board", str(board), "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "first",
        ])
        main([
            "--board", str(board), "card", "edit", cid,
            "--blocked-reason", "needs data",
        ])
        events = MarkdownBoardStore(board).events_for_card(cid)
        assert any(e.message == "Blocked reason updated via CLI" for e in events)

    def test_context_add_is_eventful(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "context", "add", cid,
            "--path", "docs/a.md", "--kind", "required",
        ])
        events = MarkdownBoardStore(board).events_for_card(cid)
        assert any("Context added: docs/a.md" in e.message for e in events)

    def test_context_rm_is_eventful(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "context", "add", cid, "--path", "a.md"])
        main(["--board", str(board), "card", "context", "rm", cid, "--path", "a.md"])
        events = MarkdownBoardStore(board).events_for_card(cid)
        assert any("Context removed: a.md" in e.message for e in events)

    def test_limit_zero_returns_nothing(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events", "--limit", "0"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "(no events)" in out

    def test_limit_zero_filtered_by_card(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _seed_events(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "events", cid, "--limit", "0"])
        assert rc == 0
        assert "(no events)" in capsys.readouterr().out

    def test_negative_limit_rejected(self, tmp_path: Path):
        board = tmp_path / "board"
        _add_card(board)
        with pytest.raises(SystemExit):
            main(["--board", str(board), "events", "--limit", "-1"])

    def test_inspection_commands_do_not_build_executor(self, tmp_path: Path, monkeypatch, capsys):
        # Regression for codex review P2: doctor/events/traces/list must not
        # call _build_executor, so they work even if the chosen executor
        # backend (e.g. --executor agentao) is unavailable on this host.
        from kanban import cli as cli_module

        def _blow_up(name: str):
            raise SystemExit(f"_build_executor called with {name} — inspection path should not trigger this")

        monkeypatch.setattr(cli_module, "_build_executor", _blow_up)

        board = tmp_path / "board"
        _add_card(board)
        capsys.readouterr()
        assert main(["--executor", "agentao", "--board", str(board), "events"]) == 0
        assert main(["--executor", "agentao", "--board", str(board), "doctor"]) == 0
        assert main(["--executor", "agentao", "--board", str(board), "list"]) == 0

    def test_acceptance_mutations_are_eventful(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "A1"])
        main(["--board", str(board), "card", "acceptance", "add", cid, "--item", "A2"])
        main(["--board", str(board), "card", "acceptance", "rm", cid, "--index", "1"])
        main(["--board", str(board), "card", "acceptance", "clear", cid])
        msgs = [e.message for e in MarkdownBoardStore(board).events_for_card(cid)]
        assert any("Acceptance criterion added: A1" == m for m in msgs)
        assert any("Acceptance criterion removed at index 1" == m for m in msgs)
        assert any("Acceptance criteria cleared via CLI" == m for m in msgs)


class TestShowOutput:
    """`kanban show` renders YAML by default and single-line JSON under --json.

    Tests parse the output rather than pinning whitespace so we're free to
    adjust the layout later without churning expectations.
    """

    def test_default_is_valid_yaml(self, tmp_path: Path, capsys):
        import yaml as _yaml

        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()  # drain the "Created card ..." line
        rc = main(["--board", str(board), "show", cid])
        assert rc == 0
        data = _yaml.safe_load(capsys.readouterr().out)
        assert isinstance(data, dict)
        assert data["id"] == cid
        assert data["title"] == "T"
        assert data["goal"] == "G"
        assert data["status"] == "inbox"
        assert data["priority"] == "MEDIUM"

    def test_json_flag_emits_single_line_json(self, tmp_path: Path, capsys):
        import json as _json_
        import yaml as _yaml

        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()

        rc = main(["--board", str(board), "show", cid, "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        # Trailing newline from print() is fine; the payload itself is one line.
        payload_line = out.rstrip("\n")
        assert "\n" not in payload_line
        json_data = _json_.loads(payload_line)

        # The JSON payload must mirror the YAML payload exactly.
        rc = main(["--board", str(board), "show", cid])
        assert rc == 0
        yaml_data = _yaml.safe_load(capsys.readouterr().out)
        assert json_data == yaml_data

    def test_shows_runtime_fields_when_set(self, tmp_path: Path, capsys):
        import yaml as _yaml
        from datetime import datetime, timezone
        from kanban.models import (
            AgentRole,
            Card,
            ContextRef,
            RevisionRequest,
        )

        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        card = store.add_card(
            Card(
                id="abcd1234-0000-0000-0000-000000000000",
                title="runtime",
                goal="exercise all fields",
                agent_profile="claude-worker",
                agent_profile_source="manual",
                worktree_branch="kanban/abcd1234",
                worktree_base_commit="9f2c1abc00",
                rework_iteration=2,
                revision_requests=[
                    RevisionRequest(
                        at=datetime(2026, 4, 16, 10, 30, tzinfo=timezone.utc),
                        from_role=AgentRole.REVIEWER,
                        iteration=1,
                        summary="add a test",
                        hints=["write tests/test_foo.py"],
                        failing_criteria=["tests exist"],
                    )
                ],
                context_refs=[
                    ContextRef(kind="required", path="docs/api.md", note="contract")
                ],
            )
        )

        rc = main(["--board", str(board), "show", card.id])
        assert rc == 0
        data = _yaml.safe_load(capsys.readouterr().out)

        assert data["agent_profile"] == "claude-worker"
        assert data["agent_profile_source"] == "manual"
        assert data["worktree_branch"] == "kanban/abcd1234"
        assert data["worktree_base_commit"] == "9f2c1abc00"
        assert data["rework_iteration"] == 2
        assert data["revision_requests"] == [
            {
                "iteration": 1,
                "from_role": "reviewer",
                "at": "2026-04-16T10:30:00Z",
                "summary": "add a test",
                "hints": ["write tests/test_foo.py"],
                "failing_criteria": ["tests exist"],
            }
        ]
        assert data["context_refs"] == [
            {"kind": "required", "path": "docs/api.md", "note": "contract"}
        ]

    def test_omits_unset_optional_fields(self, tmp_path: Path, capsys):
        import yaml as _yaml

        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "show", cid])
        assert rc == 0
        data = _yaml.safe_load(capsys.readouterr().out)

        for absent in (
            "owner_role",
            "blocked_reason",
            "blocked_at",
            "agent_profile",
            "agent_profile_source",
            "worktree_branch",
            "worktree_base_commit",
            "rework_iteration",
            "revision_requests",
            "depends_on",
            "acceptance_criteria",
            "context_refs",
            "outputs",
        ):
            assert absent not in data, f"{absent} should be omitted when unset"

    def test_yaml_uses_block_scalar_for_multiline_outputs(self, tmp_path: Path, capsys):
        from kanban.models import Card

        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        card = store.add_card(
            Card(
                title="multi",
                goal="g",
                outputs={"implementation": "line1\nline2\nline3"},
            )
        )

        rc = main(["--board", str(board), "show", card.id])
        assert rc == 0
        raw = capsys.readouterr().out
        # Multi-line outputs must render as a YAML block scalar, not a
        # single-line quoted string with embedded \n.
        assert "implementation: |" in raw
        assert "line1" in raw
        assert "line3" in raw

    def test_timestamps_use_iso_z(self, tmp_path: Path, capsys):
        import re
        import json as _json_
        import yaml as _yaml

        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()
        iso_z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

        main(["--board", str(board), "show", cid])
        y = _yaml.safe_load(capsys.readouterr().out)
        assert iso_z.fullmatch(y["created_at"])
        assert iso_z.fullmatch(y["updated_at"])

        main(["--board", str(board), "show", cid, "--json"])
        j = _json_.loads(capsys.readouterr().out)
        assert iso_z.fullmatch(j["created_at"])
        assert iso_z.fullmatch(j["updated_at"])


class TestCardIdPrefix:
    """Unique card-id prefixes should resolve to the full id.

    UUIDs are random in practice, so seed explicit ids through the store
    to pin the prefix space and exercise unique, ambiguous, and no-match
    branches deterministically.
    """

    def _seed_two(self, board: Path) -> tuple[str, str]:
        from kanban.models import Card
        store = MarkdownBoardStore(board)
        a = store.add_card(Card(id="aaaa1111-0000-0000-0000-000000000000", title="A", goal="ga"))
        b = store.add_card(Card(id="bbbb2222-0000-0000-0000-000000000000", title="B", goal="gb"))
        return a.id, b.id

    def test_show_expands_unique_prefix(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        a, _ = self._seed_two(board)
        rc = main(["--board", str(board), "show", "aaaa"])
        assert rc == 0
        out = capsys.readouterr().out
        assert a in out
        assert "ga" in out

    def test_edit_expands_unique_prefix(self, tmp_path: Path):
        board = tmp_path / "board"
        a, _ = self._seed_two(board)
        rc = main([
            "--board", str(board), "card", "edit", "aaaa", "--title", "A2",
        ])
        assert rc == 0
        assert MarkdownBoardStore(board).get_card(a).title == "A2"

    def test_ambiguous_prefix_errors(self, tmp_path: Path, capsys):
        from kanban.models import Card
        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        store.add_card(Card(id="abcd1111-0000-0000-0000-000000000000", title="A", goal="g"))
        store.add_card(Card(id="abcd2222-0000-0000-0000-000000000000", title="B", goal="g"))
        with pytest.raises(SystemExit) as exc:
            main(["--board", str(board), "show", "abcd"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Ambiguous" in err
        assert "abcd" in err

    def test_unknown_prefix_still_exits_one(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        self._seed_two(board)
        rc = main(["--board", str(board), "show", "zz"])
        assert rc == 1
        err = capsys.readouterr().err
        # Original input is echoed back so operators recognize their typo.
        assert "zz" in err

    def test_move_expands_unique_prefix(self, tmp_path: Path):
        board = tmp_path / "board"
        a, _ = self._seed_two(board)
        rc = main(["--board", str(board), "move", "aaaa", "ready"])
        assert rc == 0
        assert MarkdownBoardStore(board).get_card(a).status == CardStatus.READY

    def test_depends_expands_unique_prefix(self, tmp_path: Path):
        board = tmp_path / "board"
        a, _ = self._seed_two(board)
        rc = main([
            "--board", str(board), "card", "add",
            "--title", "Child", "--goal", "c",
            "--depends", "aaaa",
        ])
        assert rc == 0
        cards = MarkdownBoardStore(board).list_cards()
        child = next(c for c in cards if c.title == "Child")
        assert child.depends_on == [a]

    def test_depends_ambiguous_prefix_errors(self, tmp_path: Path):
        from kanban.models import Card
        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        store.add_card(Card(id="abcd1111-0000-0000-0000-000000000000", title="A", goal="g"))
        store.add_card(Card(id="abcd2222-0000-0000-0000-000000000000", title="B", goal="g"))
        with pytest.raises(SystemExit) as exc:
            main([
                "--board", str(board), "card", "add",
                "--title", "Child", "--goal", "c",
                "--depends", "abcd",
            ])
        assert exc.value.code == 2

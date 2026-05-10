"""Tests for `kanban mcp install`."""

from __future__ import annotations

from pathlib import Path

from kanban.cli import main


def test_print_emits_both_clients(tmp_path: Path, capsys):
    rc = main(["--board", str(tmp_path), "mcp", "install"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude mcp add kanban" in out
    assert "codex mcp add kanban" in out
    assert f"--board {tmp_path}" in out


def test_single_client_prints_one_line(tmp_path: Path, capsys):
    rc = main(["--board", str(tmp_path), "mcp", "install", "--client", "claude"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("claude mcp add kanban -- ")


def test_renders_uvx_when_not_in_source_checkout(tmp_path: Path, monkeypatch):
    # Point the package-root probe at a directory with no pyproject.toml so
    # the helper picks the installed-package branch even though the test
    # itself runs against the editable checkout. The probe lives in
    # ``kanban.cli.commands.misc`` and walks ``parent.parent.parent.parent``
    # to reach the project root (``misc.py → commands → cli → kanban →
    # project_root``), so we patch the misc module's ``__file__``.
    from kanban.cli.commands import misc

    monkeypatch.setattr(
        misc, "__file__", str(tmp_path / "kanban" / "cli" / "commands" / "misc.py")
    )
    from kanban import cli

    claude, codex = cli._mcp_install_args("kanban", tmp_path)
    assert claude[:6] == ["claude", "mcp", "add", "kanban", "--", "uvx"]
    assert codex[:6] == ["codex", "mcp", "add", "kanban", "--", "uvx"]
    assert "agentao-kanban" in claude
    assert "kanban-mcp" in claude


def test_renders_uv_run_when_in_source_checkout(tmp_path: Path):
    from kanban import cli

    claude, _ = cli._mcp_install_args("kanban", tmp_path)
    # `uv run --project <repo>` shape — `uv` is the launcher, not `uvx`.
    assert claude[5] == "uv"
    assert "--project" in claude


def test_relative_board_path_is_resolved(monkeypatch, tmp_path: Path):
    # MCP clients launch the server from their own cwd, so we need to
    # resolve relative paths before baking them into the registration
    # command.
    from kanban import cli

    monkeypatch.chdir(tmp_path)
    relative = Path("workspace/board")
    claude, _ = cli._mcp_install_args("kanban", relative)
    expected = str((tmp_path / "workspace" / "board").resolve())
    assert expected in claude
    assert "workspace/board" not in [
        a for a in claude if a == "workspace/board"
    ]


def test_run_with_print_client_rejects(tmp_path: Path, capsys):
    rc = main(["--board", str(tmp_path), "mcp", "install", "--run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--run requires --client" in err


def test_custom_name(tmp_path: Path, capsys):
    rc = main([
        "--board", str(tmp_path),
        "mcp", "install", "--client", "codex", "--name", "myboard",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex mcp add myboard" in out

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone as _tz
_UTC = _tz.utc
from pathlib import Path
from typing import Any

from .models import (
    AgentResult,
    AgentRole,
    Card,
    CardEvent,
    CardPriority,
    CardStatus,
    ContextRef,
    TraceInfo,
    utc_now,
)

FRONT_MATTER_DELIM = "+++"
DEFAULT_RAW_RETENTION = 5

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LOG = logging.getLogger(__name__)


def _tail(items: list[Any], limit: int | None) -> list[Any]:
    """Return the last `limit` items. `None` → all; `<=0` → none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


class MarkdownBoardStore:
    """Persist board state as one Markdown file per card under <root>/cards/.

    The TOML front-matter between ``+++`` fences is the source of truth. The
    body below is a regenerated human-readable view and is ignored on read.
    Events are appended to ``<root>/events.log`` as tab-separated lines.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        raw_root: str | os.PathLike[str] | None = None,
        raw_retention: int = DEFAULT_RAW_RETENTION,
    ) -> None:
        self.root = Path(root)
        self.cards_dir = self.root / "cards"
        self.events_path = self.root / "events.log"
        # Raw transcripts live beside the board dir by default (workspace/raw/
        # for a board at workspace/board/). workspace/ is already gitignored.
        self.raw_root = Path(raw_root) if raw_root else self.root.parent / "raw"
        self.raw_retention = raw_retention
        self.cards_dir.mkdir(parents=True, exist_ok=True)

        self._cards: dict[str, Card] = {}
        self._events: list[CardEvent] = []
        self._unparseable: list[str] = []
        self._load()

    def add_card(self, card: Card) -> Card:
        self._cards[card.id] = card
        self._write_card(card)
        self.append_event(card.id, f"Card created in {card.status.value}")
        return card

    def get_card(self, card_id: str) -> Card:
        return self._cards[card_id]

    def list_cards(self) -> list[Card]:
        return list(self._cards.values())

    def list_by_status(self, status: CardStatus) -> list[Card]:
        cards = [c for c in self._cards.values() if c.status == status]
        return sorted(cards, key=lambda c: (-int(c.priority), c.created_at))

    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card:
        card = self.get_card(card_id)
        card.status = status
        card.add_history(note, role="system")
        self._write_card(card)
        self.append_event(card_id, note)
        return card

    def update_card(self, card_id: str, **updates: object) -> Card:
        card = self.get_card(card_id)
        for key, value in updates.items():
            if key == "context_refs":
                value = [ContextRef.coerce(v) for v in value]  # type: ignore[arg-type]
            setattr(card, key, value)
        card.updated_at = utc_now()
        self._write_card(card)
        return card

    def append_event(self, card_id: str, message: str) -> None:
        event = CardEvent(card_id=card_id, message=message)
        self._events.append(event)
        record = {"at": event.at.isoformat(), "card_id": card_id, "message": message}
        self._write_event_line(record)

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        at = utc_now()
        raw_path_str: str | None = None
        if result.raw_response is not None:
            raw_path = self._write_raw_transcript(
                card_id, result.role, at, result.raw_response
            )
            if raw_path is not None:
                try:
                    raw_path_str = str(raw_path.relative_to(self.root.parent))
                except ValueError:
                    raw_path_str = str(raw_path)
        event = CardEvent(
            card_id=card_id,
            message=result.summary,
            at=at,
            role=result.role,
            prompt_version=result.prompt_version,
            duration_ms=result.duration_ms,
            attempt=result.attempt,
            raw_path=raw_path_str,
        )
        self._events.append(event)
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "card_id": card_id,
            "role": result.role.value,
            "prompt_version": result.prompt_version,
            "duration_ms": result.duration_ms,
            "attempt": result.attempt,
            "message": result.summary,
        }
        if raw_path_str is not None:
            record["raw_path"] = raw_path_str
        self._write_event_line(record)

    def list_events(self, *, limit: int | None = None) -> list[CardEvent]:
        return _tail(list(self._events), limit)

    def list_execution_events(
        self,
        *,
        card_id: str | None = None,
        role: AgentRole | None = None,
        limit: int | None = None,
    ) -> list[CardEvent]:
        events = [e for e in self._events if e.is_execution]
        if card_id is not None:
            events = [e for e in events if e.card_id == card_id]
        if role is not None:
            events = [e for e in events if e.role == role]
        return _tail(events, limit)

    def list_traces(
        self,
        card_id: str,
        *,
        role: AgentRole | None = None,
        latest: bool = False,
    ) -> list[TraceInfo]:
        card_dir = self.raw_root / card_id
        if not card_dir.exists():
            return []
        traces: list[TraceInfo] = []
        for path in sorted(card_dir.glob("*.md")):
            role_str, sep, stamp_with_ext = path.name.partition("-")
            if not sep or not stamp_with_ext.endswith(".md"):
                continue
            try:
                this_role = AgentRole(role_str)
            except ValueError:
                continue
            if role is not None and this_role != role:
                continue
            stamp_str = stamp_with_ext[:-3]
            try:
                at = datetime.strptime(stamp_str, "%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=_UTC
                )
            except ValueError:
                at = datetime.fromtimestamp(path.stat().st_mtime, tz=_UTC)
            traces.append(
                TraceInfo(
                    card_id=card_id,
                    role=this_role,
                    at=at,
                    path=str(path),
                    size=path.stat().st_size,
                )
            )
        if latest and traces:
            return [max(traces, key=lambda t: t.at)]
        return traces

    def _write_event_line(self, record: dict[str, Any]) -> None:
        # O_APPEND writes are atomic for < PIPE_BUF on POSIX, so concurrent
        # CLI + daemon writers don't interleave whole lines.
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def _write_raw_transcript(
        self,
        card_id: str,
        role: AgentRole,
        at: datetime,
        raw_response: str,
    ) -> Path | None:
        if self.raw_retention <= 0:
            return None
        card_dir = self.raw_root / card_id
        card_dir.mkdir(parents=True, exist_ok=True)
        stamp = at.strftime("%Y%m%dT%H%M%S%fZ")
        path = card_dir / f"{role.value}-{stamp}.md"
        path.write_text(raw_response, encoding="utf-8")
        # Retention: keep the most recent N per (card, role).
        role_files = sorted(card_dir.glob(f"{role.value}-*.md"))
        for stale in role_files[: -self.raw_retention]:
            try:
                stale.unlink()
            except OSError:
                pass
        return path

    def events_for_card(self, card_id: str) -> list[CardEvent]:
        return [e for e in self._events if e.card_id == card_id]

    def board_snapshot(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for card in self.list_cards():
            grouped[card.status.value].append(card.title)
        return dict(grouped)

    # ---------- internals ----------

    def _card_path(self, card_id: str) -> Path:
        return self.cards_dir / f"{card_id}.md"

    def _load(self) -> None:
        for path in sorted(self.cards_dir.glob("*.md")):
            # Valid TOML can still miss fields the Card constructor requires
            # (TypeError) or reject a value type (KeyError/ValueError). Treat
            # any failure in this reader path as an unparseable card rather
            # than letting a single bad file break the whole board.
            try:
                card = _read_card(path)
            except (tomllib.TOMLDecodeError, TypeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable card %s: %s", path.name, exc)
                self._unparseable.append(path.name)
                continue
            self._cards[card.id] = card
        if self.events_path.exists():
            self._load_events()

    def unparseable_cards(self) -> list[str]:
        return list(self._unparseable)

    def _load_events(self) -> None:
        with self.events_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                event = _decode_event_line(line)
                if event is not None:
                    self._events.append(event)

    def _write_card(self, card: Card) -> None:
        path = self._card_path(card.id)
        tmp = path.with_suffix(".md.tmp")
        content = _render_card(card)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)


# ---------- event decoding ----------


def _decode_event_line(line: str) -> CardEvent | None:
    if line.startswith("{"):
        try:
            data = json.loads(line)
            role_str = data.get("role")
            role = AgentRole(role_str) if role_str else None
            return CardEvent(
                card_id=str(data["card_id"]),
                message=str(data["message"]),
                at=datetime.fromisoformat(str(data["at"])),
                role=role,
                prompt_version=data.get("prompt_version"),
                duration_ms=data.get("duration_ms"),
                attempt=data.get("attempt"),
                raw_path=data.get("raw_path"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None
    # Backward compat: legacy TSV lines `<iso>\t<card_id>\t<message>`.
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    ts, card_id, message = parts
    try:
        at = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return CardEvent(card_id=card_id, message=message, at=at)


# ---------- serialization helpers ----------

_CARD_FIELD_NAMES = {f.name for f in fields(Card)}


def _render_card(card: Card) -> str:
    fm = _dump_toml(_card_to_toml_dict(card))
    body = _render_body(card)
    return f"{FRONT_MATTER_DELIM}\n{fm}{FRONT_MATTER_DELIM}\n\n{body}"


def _card_to_toml_dict(card: Card) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": card.id,
        "title": card.title,
        "status": card.status.value,
        "priority": int(card.priority),
        "goal": card.goal,
        "acceptance_criteria": list(card.acceptance_criteria),
        "context_refs": [
            {"path": r.path, "kind": r.kind, "note": r.note} for r in card.context_refs
        ],
        "depends_on": list(card.depends_on),
        "history": list(card.history),
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }
    if card.owner_role is not None:
        data["owner_role"] = card.owner_role.value
    if card.blocked_reason is not None:
        data["blocked_reason"] = card.blocked_reason
    if card.outputs:
        data["outputs"] = dict(card.outputs)
    return data


def _render_body(card: Card) -> str:
    lines: list[str] = [f"# {card.title}", "", "## Goal", "", card.goal, ""]
    if card.acceptance_criteria:
        lines += ["## Acceptance Criteria", ""]
        lines += [f"- {item}" for item in card.acceptance_criteria]
        lines.append("")
    if card.context_refs:
        lines += ["## Context", ""]
        for ref in card.context_refs:
            suffix = f" — {ref.note}" if ref.note else ""
            lines.append(f"- [{ref.kind}] `{ref.path}`{suffix}")
        lines.append("")
    if card.outputs:
        lines += ["## Outputs", ""]
        for key, value in card.outputs.items():
            lines += [f"### {key}", "", str(value), ""]
    if card.history:
        lines += ["## History", ""]
        lines += [f"- {item}" for item in card.history]
        lines.append("")
    return "\n".join(lines)


def _read_card(path: Path) -> Card:
    text = path.read_text(encoding="utf-8")
    fm = _extract_front_matter(text, path)
    data = tomllib.loads(fm)
    return _card_from_toml_dict(data)


def _extract_front_matter(text: str, path: Path) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        raise ValueError(f"Missing front-matter opener in {path}")
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONT_MATTER_DELIM:
            return "\n".join(lines[1:i]) + "\n"
    raise ValueError(f"Unclosed front-matter in {path}")


def _card_from_toml_dict(data: dict[str, Any]) -> Card:
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in _CARD_FIELD_NAMES:
            continue
        if key == "status":
            kwargs[key] = CardStatus(value)
        elif key == "priority":
            kwargs[key] = CardPriority(int(value))
        elif key == "owner_role":
            kwargs[key] = AgentRole(value) if value is not None else None
        elif key == "context_refs":
            coerced: list[ContextRef] = []
            for raw in value:
                ref = ContextRef.try_coerce(raw)
                if ref is None:
                    _LOG.warning(
                        "Dropping malformed context_ref in card %s: %r",
                        data.get("id", "<unknown>"),
                        raw,
                    )
                    continue
                coerced.append(ref)
            kwargs[key] = coerced
        else:
            kwargs[key] = value
    return Card(**kwargs)


# ---------- minimal TOML dumper (covers the types we use) ----------


def _dump_toml(data: dict[str, Any]) -> str:
    top: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            top.append(f"{_toml_key(key)} = {_toml_value(value)}")
    out = "\n".join(top)
    if out:
        out += "\n"
    for name, table in tables:
        out += f"\n[{_toml_key(name)}]\n"
        for k, v in table.items():
            out += f"{_toml_key(k)} = {_toml_value(v)}\n"
    return out


def _toml_key(name: str) -> str:
    """Return a TOML-safe key: bare if it matches [A-Za-z0-9_-]+, quoted otherwise.

    Agent-supplied outputs can carry dict keys with dots, unicode, or spaces
    (e.g. filenames like "test_report.xlsx"), which break bare-key syntax and
    create dotted-key nesting. Quoting keeps round-trip stable.
    """
    if _BARE_KEY_RE.match(name):
        return name
    return _toml_string(name, inline=True)


def _toml_value(value: Any, *, inline: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        # Inline tables must stay single-line per TOML 1.0.
        return _toml_inline_table(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item, inline=inline) for item in value) + "]"
    if isinstance(value, str):
        return _toml_string(value, inline=inline)
    return _toml_string(str(value), inline=inline)


def _toml_inline_table(data: dict[str, Any]) -> str:
    parts = [
        f"{_toml_key(k)} = {_toml_value(v, inline=True)}" for k, v in data.items()
    ]
    return "{ " + ", ".join(parts) + " }"


def _toml_string(value: str, *, inline: bool = False) -> str:
    if "\n" in value and not inline:
        escaped = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        return f'"""\n{escaped}"""'
    escaped = (
        value.replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'

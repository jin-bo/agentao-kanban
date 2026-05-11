from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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

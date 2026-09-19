"""Template filters and globals shared by every blueprint."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Flask

from .db import parse_iso
from .usage import truncate


def register_template_helpers(app: Flask) -> None:
    @app.template_filter("tokens")
    def tokens(value) -> str:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return "0"
        if abs(number) >= 1_000_000_000:
            return f"{number / 1_000_000_000:.2f}B"
        if abs(number) >= 1_000_000:
            return f"{number / 1_000_000:.2f}M"
        if abs(number) >= 1_000:
            return f"{number / 1_000:.1f}K"
        return f"{number:,}"

    @app.template_filter("comma")
    def comma(value) -> str:
        try:
            return f"{int(value or 0):,}"
        except (TypeError, ValueError):
            return "0"

    @app.template_filter("datetime")
    def fmt_datetime(value) -> str:
        dt = parse_iso(value) if isinstance(value, str) else value
        if not isinstance(dt, datetime):
            return "—"
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @app.template_filter("relative")
    def fmt_relative(value) -> str:
        dt = parse_iso(value) if isinstance(value, str) else value
        if not isinstance(dt, datetime):
            return "never"
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        if seconds < 604800:
            return f"{seconds // 86400}d ago"
        return dt.strftime("%Y-%m-%d")

    @app.template_filter("ms")
    def fmt_ms(value) -> str:
        try:
            ms = int(value or 0)
        except (TypeError, ValueError):
            return "—"
        if ms >= 1000:
            return f"{ms / 1000:.2f}s"
        return f"{ms}ms"

    @app.template_filter("preview")
    def fmt_preview(value) -> str:
        return truncate(value, 220) or ""

    app.jinja_env.globals["app_version"] = "0.1.0"

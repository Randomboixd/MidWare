"""Template filters and globals shared by every blueprint."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Flask

from .db import parse_iso
from .usage import truncate
from . import __version__


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

    @app.template_filter("size")
    def fmt_size(value) -> str:
        """A byte count as a short human figure (the payload block's caption)."""
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return "0 B"
        if number < 1024:
            return f"{number} B"
        for unit in ("KB", "MB"):
            number /= 1024.0
            if number < 1024 or unit == "MB":
                return f"{number:,.1f} {unit}"
        return f"{number:,.1f} MB"

    @app.template_filter("money")
    def fmt_money(value, currency: str | None = "USD") -> str:
        """A cost with its symbol: sub-dollar figures keep four decimals.

        Four decimals because a single proxied call routinely costs less than a
        cent, and rounding that to ``$0.00`` is the same as not showing it.
        """
        if value is None:
            return "none"
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "none"
        digits = 4 if 0 < abs(amount) < 1 else 2
        code = (currency or "USD").upper()
        symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(code)
        if symbol:
            return f"{symbol}{amount:,.{digits}f}"
        return f"{amount:,.{digits}f} {code}"

    @app.template_filter("pct")
    def fmt_pct(value) -> str:
        if value is None:
            return "none"
        try:
            return f"{float(value):.1f}%"
        except (TypeError, ValueError):
            return "none"

    app.jinja_env.globals["app_version"] = __version__

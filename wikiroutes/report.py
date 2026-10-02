"""Отчёт о ходе конвейера: сбор сообщений стадий и рендер итогов.

Сообщения стадий собираются в ``Reporter.messages``; при ``echo=True``
дублируются в консоль в реальном времени. Итоговая печать выполняется
потребителем (CLI) из результата конвейера, а не внутри расчётов;
``render_errors`` — единственный рендер финального блока ошибок.
"""

from __future__ import annotations

from collections.abc import Sequence

from .enums import type_label
from .models import RouteData

__all__ = ["Reporter", "render_errors"]


class Reporter:
    """Собирает сообщения стадий; при ``echo=True`` дублирует их в консоль."""

    def __init__(self, *, echo: bool = True) -> None:
        self._echo = echo
        self.messages: list[str] = []

    def line(self, text: str = "") -> None:
        """Записывает сообщение стадии (и выводит в консоль при ``echo``)."""
        self.messages.append(text)

        if self._echo:
            print(text)


def render_errors(bad_routes: Sequence[RouteData], limit: int = 5) -> str:
    """Рендерит финальный блок ошибок загрузки для консоли."""
    if not bad_routes:
        return ""

    lines = [f"⚠ Ошибки ({len(bad_routes)}):"]
    lines.extend(
        f"    {type_label(error_route.route_type)} "
        f"{error_route.name}: {error_route.error}"
        for error_route in bad_routes[:limit]
    )

    return "\n".join(lines)

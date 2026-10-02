"""Разрешение версии набора Overture.

Resolver должен возвращать конкретный идентификатор release. Значения
latest/current не являются release и никогда не используются как ключи кэша.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
from typing import Final

_LATEST_ALIASES: Final[frozenset[str]] = frozenset({"latest", "current"})
_RELEASE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:\.(\d+))?(?!\d)"
)
_STAC_CATALOG_HOSTS: Final[tuple[str, ...]] = (
    "https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/stac",
    "https://stac.overturemaps.org",
)
_STAC_TIMEOUT_S = 30.0

logger = logging.getLogger("wikiroutes.gis.overture")


class OvertureReleaseError(RuntimeError):
    """Не удалось определить конкретный release Overture."""


def _release_sort_key(value: str) -> tuple[int, int, int, int, str]:
    """Сортировочный ключ для release вида YYYY-MM-DD.N."""
    match = _RELEASE_RE.search(value)
    if match is None:
        return (0, 0, 0, -1, value)

    year, month, day, revision = match.groups()
    return (
        int(year),
        int(month),
        int(day),
        int(revision or 0),
        value,
    )


def _release_from_href(href: str) -> str | None:
    """Извлекает release из STAC child href."""
    matches = list(_RELEASE_RE.finditer(href))
    if matches:
        return matches[-1].group(0)

    tail = href.rstrip("/").rsplit("/", 1)[-1]
    if tail in _LATEST_ALIASES or tail.lower() == "catalog.json":
        return None
    return tail or None


def _stac_release_candidates(catalog: dict) -> list[str]:
    """Собирает все release-кандидаты из child-ссылок STAC."""
    candidates: set[str] = set()

    for link in catalog.get("links", []):
        if not isinstance(link, dict) or link.get("rel") != "child":
            continue

        href = link.get("href")
        if isinstance(href, str):
            release_id = _release_from_href(href)
            if release_id:
                candidates.add(release_id)

        for field in ("id", "title"):
            value = link.get(field)
            if isinstance(value, str):
                match = _RELEASE_RE.search(value)
                if match:
                    candidates.add(match.group(0))

    return sorted(candidates, key=_release_sort_key, reverse=True)


def _resolve_via_stac() -> str:
    """Определяет самый новый release из доступных STAC-каталогов."""
    errors: list[Exception] = []

    for host in _STAC_CATALOG_HOSTS:
        url = f"{host}/catalog.json"
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "wikiroutes-overture/1",
                    "Connection": "close",
                },
            )
            with urllib.request.urlopen(
                request,
                timeout=_STAC_TIMEOUT_S,
                context=ssl.create_default_context(),
            ) as response:
                catalog = json.loads(response.read())

            candidates = _stac_release_candidates(catalog)
            if candidates:
                selected = candidates[0]
                logger.info(
                    "Overture: release автоматически определён через STAC: %s "
                    "(кандидатов: %d)",
                    selected,
                    len(candidates),
                )
                return selected

            errors.append(
                OvertureReleaseError(
                    f"STAC-каталог {url} не содержит release child-ссылок"
                )
            )
        except Exception as exc:  # noqa: BLE001 — внешняя сеть
            errors.append(exc)

    detail = f": {errors[-1]}" if errors else ""
    raise OvertureReleaseError(
        f"Не удалось определить текущий release Overture через STAC{detail}"
    )


def resolve_overture_release(release: str | None = None) -> str:
    """Возвращает конкретный release Overture.

    Приоритет:
      1. явно переданный release;
      2. OVERTURE_RELEASE из окружения;
      3. overturemaps.core.get_latest_release();
      4. самый новый release из STAC.

    latest/current всегда разрешаются в конкретный release.
    """
    value = release
    if value is None:
        value = os.getenv("OVERTURE_RELEASE")

    value = value.strip() if isinstance(value, str) else value
    if value and value.lower() not in _LATEST_ALIASES:
        return value

    try:
        import overturemaps

        resolved = str(overturemaps.core.get_latest_release()).strip()
        if resolved:
            logger.info("Overture: release автоматически определён: %s", resolved)
            return resolved
    except Exception as exc:  # noqa: BLE001 — опциональный пакет; ниже STAC fallback
        logger.debug("Overture: overturemaps get_latest_release недоступен: %s", exc)

    return _resolve_via_stac()


__all__ = [
    "OvertureReleaseError",
    "_LATEST_ALIASES",
    "_RELEASE_RE",
    "_release_sort_key",
    "_release_from_href",
    "_stac_release_candidates",
    "resolve_overture_release",
]

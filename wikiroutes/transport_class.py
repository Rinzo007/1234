import re
from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup


def _stringify_class(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, Mapping):
        parts: list[str] = []

        low = value.get("min", value.get("from", value.get("capacityMin")))
        high = value.get("max", value.get("to", value.get("capacityMax")))

        if low is not None and high is not None:
            parts.append(f"{low}-{high}")

        name = value.get("name", value.get("title", ""))

        if name:
            parts.append(str(name))

        return " ".join(part for part in parts if part).strip()

    if isinstance(value, Sequence) and not isinstance(value, str):
        return "; ".join(item for item in (_stringify_class(x) for x in value) if item)

    return ""


def extract_transport_class(payload: Any) -> str:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                key_lower = str(key).lower()

                if any(
                    token in key_lower
                    for token in ("class", "класс", "capacity", "вместим")
                ):
                    text = _stringify_class(value)

                    if text:
                        found.append(text)
                else:
                    walk(value)

        elif isinstance(obj, Sequence) and not isinstance(obj, str):
            for item in obj:
                walk(item)

    walk(payload)

    return found[0] if found else ""


def parse_transport_class(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 — невалидный HTML не должен ломать извлечение
        return ""

    label = soup.find(string=re.compile(r"Классы\s+транспорта"))

    if not label:
        return ""

    node: Any = label

    for _ in range(4):
        node = node.parent if node else None

        if node is None:
            break

    if node is None:
        return ""

    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    match = re.search(r"Классы\s+транспорта:?\s*(.+)", text)

    if match and match.group(1).strip() and match.group(1).strip() != "-":
        return match.group(1).strip()

    return ""


def mentions_electrobus(text: str) -> bool:
    """True, если текст содержит слово «электробус» (без учёта регистра)."""
    return "электробус" in text.lower()


def detect_electrobus(payload: Any, page_text: str | None = None) -> bool:
    """Определяет, относится ли маршрут к электробусам.

    Сначала проверяются структурированные поля payload API: теги
    транспорта (``transportTags``), классы транспорта (``transportClasses``),
    название, перевозчик и примечание. Затем, при наличии, — текст страницы
    маршрута (например, комментарии или описание).
    """
    candidates: list[Any] = []

    if isinstance(payload, dict):
        if payload.get("is_electrobus"):
            return True

        candidates.extend(
            [
                payload.get("name"),
                payload.get("company"),
                payload.get("note"),
            ]
        )

        for group in ("transportClasses", "transportTags"):
            entries = payload.get(group) or []

            if isinstance(entries, list):
                candidates.extend(
                    entry.get("name") for entry in entries if isinstance(entry, dict)
                )

    if any(isinstance(item, str) and mentions_electrobus(item) for item in candidates):
        return True

    return page_text is not None and mentions_electrobus(page_text)

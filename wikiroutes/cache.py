"""Потокобезопасный файловый JSON-кэш с ключами на основе SHA-256."""

import contextlib
import gzip
import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("wikiroutes.cache")

DEFAULT_MAX_ENTRY_BYTES = 8 * 1024 * 1024
DEFAULT_COMPRESS_MIN_BYTES = 512

#: Лимиты отдельных видов кэша. ``od_roads`` хранит граф дорожной сети города
#: и по размеру регулярно превышает общий предел — у него свой, увеличенный.
#: Предел держится в кэше, а не в вызывающем коде, потому что он является
#: свойством вида данных: иначе каждый новый вызов ``JsonCache`` без явных
#: ``kind_limits`` молча отбрасывал бы записи OD-графа.
DEFAULT_KIND_LIMITS: dict[str, int] = {
    "od_roads": 64 * 1024 * 1024,
    "osm_routes": 32 * 1024 * 1024,
}

_GZIP_MAGIC = b"\x1f\x8b"


class JsonCache:
    """Потокобезопасный файловый JSON-кэш с ключами на основе SHA-256.

    Запись выполняется атомарно (уникальный временный файл + ``os.replace``),
    поэтому конкурентное чтение никогда не видит частично записанные данные.
    При одновременной записи одного ключа выигрывает последний полный дамп.
    Размер отдельной записи ограничен ``max_entry_bytes`` (по несжатому размеру).

    Крупные записи (начиная с ``compress_min_bytes``) сжимаются gzip — файлы
    занимают заметно меньше места. Сжатие прозрачно: читатель определяет формат
    по магическим байтам, поэтому старые несжатые записи продолжают читаться.
    """

    def __init__(
        self,
        directory: Path | str = "wikiroutes_cache",
        *,
        read: bool = True,
        write: bool = True,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        kind_limits: dict[str, int] | None = None,
        compress: bool = True,
        compress_min_bytes: int = DEFAULT_COMPRESS_MIN_BYTES,
    ) -> None:
        if max_entry_bytes <= 0:
            raise ValueError("max_entry_bytes должен быть положительным")
        if compress_min_bytes < 0:
            raise ValueError("compress_min_bytes не может быть отрицательным")

        self.root = Path(directory)
        self.read_enabled = read
        self.write_enabled = write
        self.max_entry_bytes = max_entry_bytes
        # Явные лимиты вызывающего кода дополняют видовые по умолчанию,
        # а не заменяют их: иначе ``kind_limits={"osm_routes": ...}``
        # возвращал бы od_roads к общему пределу.
        self.kind_limits = {**DEFAULT_KIND_LIMITS, **(kind_limits or {})}
        self.compress = compress
        self.compress_min_bytes = compress_min_bytes
        self._lock = threading.Lock()
        self._made_dirs: set[Path] = set()

    @staticmethod
    def _validate_kind(kind: str) -> None:
        invalid = (
            not kind
            or kind in {".", ".."}
            or "/" in kind
            or "\\" in kind
            or kind != Path(kind).name
        )
        if invalid:
            raise ValueError(f"JsonCache: недопустимый kind: {kind!r}")

    def _path(self, kind: str, key: str) -> Path:
        self._validate_kind(kind)
        digest = hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()
        return self.root / kind / f"{digest}.json"

    def _ensure_parent(self, path: Path) -> None:
        parent = path.parent
        if parent in self._made_dirs:
            return
        with self._lock:
            if parent in self._made_dirs:
                return
            parent.mkdir(parents=True, exist_ok=True)
            self._made_dirs.add(parent)

    def get(self, kind: str, key: str) -> Any | None:
        if not self.read_enabled:
            return None

        path = self._path(kind, key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("Cache read failed for %s/%s: %s", kind, key, exc)
            return None
        try:
            if raw.startswith(_GZIP_MAGIC):
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("Cache read failed for %s/%s: %s", kind, key, exc)
            return None

    def put(self, kind: str, key: str, obj: Any) -> None:
        if not self.write_enabled:
            return

        path = self._path(kind, key)
        self._ensure_parent(path)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")

        try:
            # Компактный JSON (без отступов) — меньше байт и быстрее сериализация.
            payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            raw_size = len(payload.encode("utf-8"))

            limit = self.kind_limits.get(kind, self.max_entry_bytes)
            if raw_size > limit:
                logger.warning(
                    "Cache entry %s/%s skipped: %d bytes exceeds limit %d",
                    kind,
                    key,
                    raw_size,
                    limit,
                )
                return

            if self.compress and raw_size >= self.compress_min_bytes:
                payload = gzip.compress(payload.encode("utf-8"), compresslevel=6)

            # Глобальная блокировка не нужна: ``os.replace`` атомарен,
            # читатели увидят либо старую, либо новую запись целиком.
            if isinstance(payload, bytes):
                tmp.write_bytes(payload)
            else:
                tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except (OSError, TypeError, ValueError):
            logger.exception("Cache write failed for %s/%s", kind, key)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _compact_one(path: Path, compress_min_bytes: int, stats: dict[str, int]) -> None:
    """Пережимает один файл кэша; пополняет счётчики статистики на месте."""
    stats["scanned"] += 1
    try:
        raw = path.read_bytes()
    except OSError:
        stats["skipped"] += 1
        return
    if raw.startswith(_GZIP_MAGIC):
        stats["already"] += 1
        return
    if len(raw) < compress_min_bytes:
        stats["too_small"] += 1
        return
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        stats["skipped"] += 1
        return

    try:
        payload = gzip.compress(raw, compresslevel=6)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    except OSError:
        logger.exception("Cache compact failed for %s", path)
        stats["skipped"] += 1
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return
    stats["compressed"] += 1


def _compact_kind_dirs(root: Path, kinds: set[str] | None, compress_min_bytes: int) -> dict[str, int]:
    """Обходит все виды кэша, пережимая подходящие записи."""
    stats = {"scanned": 0, "compressed": 0, "already": 0, "too_small": 0, "skipped": 0}
    for kind in sorted(p.name for p in root.iterdir() if p.is_dir()):
        if kinds is not None and kind not in kinds:
            continue
        try:
            JsonCache._validate_kind(kind)
        except ValueError:
            continue

        kind_dir = root / kind
        for path in kind_dir.glob("*.json"):
            _compact_one(path, compress_min_bytes, stats)
    return stats


def compact_cache(
    directory: Path | str = "wikiroutes_cache",
    *,
    compress_min_bytes: int = DEFAULT_COMPRESS_MIN_BYTES,
    kinds: set[str] | None = None,
) -> dict[str, int]:
    """Пережимает существующие записи кэша на диске gzip (миграция формата).

    Проходит по подкаталогам-видам кэша и записывает сжатыми несжатые записи
    размером не меньше ``compress_min_bytes``. Уже сжатые файлы (начинаются с
    ``_GZIP_MAGIC``), мелкие записи и не-JSON файлы не трогаются. Замена файла
    атомарная (``os.replace``), поэтому конкурентные читатели не видят части.

    Возвращает статистику:
        scanned    — всего просмотрено файлов ``*.json``,
        compressed — пережато gzip,
        already    — уже было сжато,
        too_small  — меньше порога сжатия,
        skipped    — пропущено по другим причинам (не-JSON/ошибки чтения).
    """
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Каталог кэша не найден: {root}")
    return _compact_kind_dirs(root, kinds, compress_min_bytes)

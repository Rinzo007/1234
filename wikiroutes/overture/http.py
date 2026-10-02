"""Сетевой слой Overture: HTTP/S3, СТЭК-резолвинг, докачка и чтение частей.
Скачивание данных Overture обходит нестабильный pyarrow-S3 транспортом
напрямую по HTTP (Range-чанки, переживающие TLS-обрывы), затем парт-файлы
читаются с локального диска через geopandas/pyarrow.
"""
import contextlib
import datetime as datetime_module
import email.utils
import hashlib
import io
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("wikiroutes.gis.overture")

# Официальные endpoint'ы Overture. Azure добавлен как резервное зеркало.
_OVERTURE_HTTP_HOSTS: tuple[str, ...] = (
    "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com",
    "https://overturemaps-us-west-2.s3.amazonaws.com",
    "https://s3.us-west-2.amazonaws.com/overturemaps-us-west-2",
    "https://overturemapswestus2.blob.core.windows.net",
    "https://overturemapswestus2.dfs.core.windows.net",
)

def _overture_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

# Порция данных на один HTTP-запрос для докачки.
_OVERTURE_CHUNK_BYTES = max(1, _overture_env_int("OVERTURE_CHUNK_BYTES", 8 * 1024 * 1024))

# Пауза между повторами одного чанка после TLS-обрыва.
_CHUNK_RETRY_DELAY_S = 2.0

# Таймаут одного STAC-запроса. Метаданные (collection.json, Item JSON) — это
# маленькие JSON; длинный таймаут на них только замедляет переключение на
# следующее зеркало. Держим отдельный короткий таймаут для метаданных.
_STAC_TIMEOUT_S = 120.0
_STAC_META_TIMEOUT_S = max(10.0, _overture_env_int("OVERTURE_STAC_META_TIMEOUT_S", 30.0))
_STAC_CHUNK_BYTES = max(1, _overture_env_int("OVERTURE_STAC_CHUNK_BYTES", 4 * 1024 * 1024))

# STAC endpoint'ы. Приоритет — официальный extras-бакет S3 (каталог публикуется
# там как в S3, так и через CloudFront): он стабильнее медленного CDN и не
# режется прокси. CloudFront остаётся последним зеркалом.
_STAC_HTTP_HOSTS: tuple[str, ...] = (
    "https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/stac",
    "https://overturemaps-extras-us-west-2.s3.amazonaws.com/stac",
    "https://s3.us-west-2.amazonaws.com/overturemaps-extras-us-west-2/stac",
    "https://stac.overturemaps.org",
)

# Настройки параллелизма
_OVERTURE_SEGMENT_BYTES = max(
    _OVERTURE_CHUNK_BYTES,
    _overture_env_int("OVERTURE_SEGMENT_BYTES", 16 * 1024 * 1024),
)
_OVERTURE_SEGMENT_WORKERS = max(1, _overture_env_int("OVERTURE_SEGMENT_WORKERS", 4))
_OVERTURE_PART_WORKERS = max(1, _overture_env_int("OVERTURE_PART_WORKERS", 8))
_OVERTURE_READ_WORKERS = max(1, _overture_env_int("OVERTURE_READ_WORKERS", 16))
_STAC_ITEM_WORKERS = max(1, _overture_env_int("OVERTURE_STAC_ITEM_WORKERS", 16))
_STAC_ITEM_JSON_LIMIT = max(0, _overture_env_int("OVERTURE_STAC_ITEM_JSON_LIMIT", 24))

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$")


class _ParallelRangeFallback(Exception):
    """Сервер перестал поддерживать Range во время параллельной докачки."""


# ===== Утилиты =====

def _sql_literal(value: str) -> str:
    """Экранирует строку для вставки в SQL-литерал DuckDB."""
    return "'" + value.replace("'", "''") + "'"


# ===== Базовые HTTP-запросы =====

def _http_get_url(
    url: str, timeout: float, context: ssl.SSLContext | None = None
) -> bytes:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Connection", "close")
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.read()


def _parse_content_range(
    content_range: str, expected_start: int
) -> int | None:
    """Разбирает `Content-Range` 206-ответа; возвращает total_size или None."""
    match = _CONTENT_RANGE_RE.match(content_range.strip())
    if not match:
        return None
    range_start, range_end, total = map(int, match.groups())
    if range_start != expected_start or range_end < range_start:
        raise OSError(
            f"Некорректный Content-Range: {content_range!r}, "
            f"ожидался offset {expected_start}"
        )
    return total


def _http_get_range(
    url: str, start: int, timeout: float, chunk: int, context: ssl.SSLContext | None
) -> tuple[int, bytes, int | None]:
    """GET `bytes=start-` на URL; возвращает (статус, байты, полный размер)."""
    req = urllib.request.Request(url, method="GET")
    end = start + max(1, chunk) - 1
    req.add_header("Range", f"bytes={start}-{end}")
    req.add_header("Connection", "close")
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        status = getattr(resp, "status", 200)
        data = resp.read(chunk if status == 206 else None)
        total_size: int | None = None
        if status == 206:
            content_range = resp.headers.get("Content-Range")
            if content_range:
                total_size = _parse_content_range(content_range, start)
        else:
            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    total_size = int(content_length)
                except ValueError:
                    total_size = None
    return status, data, total_size


def _overture_host_url(host: str, bucket: str, bucket_key: str, obj_path: str) -> str:
    """Строит HTTP URL для S3/Azure endpoint'а из общего object key."""
    if host.endswith((".blob.core.windows.net", ".dfs.core.windows.net")):
        return f"{host}/{obj_path}"
    if "s3.us-west-2.amazonaws.com/" in host and not host.endswith(bucket):
        return f"{host}/{obj_path}"
    if bucket in host:
        return f"{host}/{obj_path}"
    return f"{host}/{bucket_key}"


def _fetch_chunk(
    urls: str | list[str],
    start: int,
    timeout: float,
    chunk: int,
    retries: int,
    retry_delay: float = 2.0,
) -> tuple[int, bytes, int | None]:
    """Один Range-GET на URL с повторами; при исчерпании — последняя ошибка."""
    url_list = [urls] if isinstance(urls, str) else list(urls)
    attempts = max(1, retries + 1) * len(url_list)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(retry_delay)
        url = url_list[attempt % len(url_list)]
        try:
            context = ssl.create_default_context()
            return _http_get_range(url, start, timeout, chunk, context)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "Overture: Range-чанк %s (offset %d) failed: %s",
                url,
                start,
                exc,
            )
    raise last_exc if last_exc is not None else RuntimeError(
        f"Overture: Range-загрузка {url_list[0]} не удалась"
    )


def _append_chunk(part: Path, start: int, data: bytes, chunk: int) -> int:
    """Записывает чанк в part (wb/ab по смещению) и возвращает новое смещение."""
    mode = "ab" if start > 0 else "wb"
    with part.open(mode) as fh:
        fh.write(data)
    return start + len(data)


# ===== Прогресс загрузки =====

class _PartProgress:
    """Показывает текущую скорость, прогресс и среднюю скорость загрузки."""
    _MB = 1024 * 1024

    def __init__(
        self,
        name: str,
        *,
        initial_bytes: int = 0,
        total_bytes: int | None = None,
        log_interval_s: float = 5.0,
    ) -> None:
        self._name = name
        # ``log_interval_s=0`` означает «логировать каждое обновление», а не
        # «никогда». Идиома ``or float("inf")`` ошибочно считала 0 ложным
        # значением; отсутствие интервала записывается только как ``None``.
        self._log_interval_s = (
            float("inf") if log_interval_s is None else log_interval_s
        )
        self._t0 = time.monotonic()
        self._last_log = self._t0
        self._last_bytes = initial_bytes
        self._bytes = initial_bytes
        self._total_bytes = total_bytes

    def set_total(self, total_bytes: int | None) -> None:
        if total_bytes is not None and total_bytes >= self._bytes:
            self._total_bytes = total_bytes

    def update(self, data: bytes) -> None:
        self._bytes += len(data)
        now = time.monotonic()
        if now - self._last_log >= self._log_interval_s:
            current_speed = self._current_speed(now)
            logger.info(
                "Overture: %s — %s",
                self._name,
                self._format_status(now, current_speed),
            )
            self._last_log = now
            self._last_bytes = self._bytes

    def _current_speed(self, now: float) -> float:
        elapsed = now - self._last_log
        if elapsed <= 0:
            return 0.0
        return ((self._bytes - self._last_bytes) / self._MB) / elapsed

    def _average_speed(self, now: float) -> float:
        elapsed = now - self._t0
        if elapsed <= 0:
            return 0.0
        return (self._bytes / self._MB) / elapsed

    def _format_eta(
        self, downloaded_mb: float, total_mb: float, speed: float
    ) -> str:
        if speed <= 0:
            return ""
        remaining_mb = max(0.0, total_mb - downloaded_mb)
        eta = remaining_mb / speed
        return f", осталось {remaining_mb:.1f} МБ, ETA {eta:.1f} с"

    def _format_status(self, now: float, speed: float) -> str:
        downloaded_mb = self._bytes / self._MB
        if not self._total_bytes:
            return f"скачано {downloaded_mb:.1f} МБ, скорость {speed:.2f} МБ/с"
        total_mb = self._total_bytes / self._MB
        percent = min(100.0, self._bytes * 100.0 / self._total_bytes)
        eta_text = self._format_eta(downloaded_mb, total_mb, speed)
        return (
            f"{downloaded_mb:.1f}/{total_mb:.1f} МБ ({percent:.1f}%), "
            f"скорость {speed:.2f} МБ/с{eta_text}"
        )

    def summary(self) -> str:
        now = time.monotonic()
        return (
            f"{self._name}: {self._bytes / self._MB:.1f} МБ "
            f"за {now - self._t0:.1f} с "
            f"(средняя скорость {self._average_speed(now):.2f} МБ/с)"
        )


# ===== Целостность part-файлов =====

def _sha256_file(path: Path) -> str:
    """Считает SHA-256 файла потоково."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _part_manifest_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _write_part_manifest(path: Path, *, key: str, size: int, sha256: str) -> None:
    """Атомарно сохраняет метаданные завершённой части."""
    target = _part_manifest_path(path)
    tmp = target.with_name(target.name + ".tmp")
    payload = {
        "version": 1,
        "key": key,
        "size": int(size),
        "sha256": sha256,
    }
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _read_part_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_part_manifest_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parquet_ok(path: Path) -> bool:
    """Проверяет, что файл существует, непустой и открывается как parquet."""
    try:
        from pyarrow.parquet import ParquetFile
        if path.stat().st_size <= 0:
            return False
        ParquetFile(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_valid_cached_part(
    path: Path,
    *,
    key: str | None = None,
) -> bool:
    """Проверяет parquet-footer, размер и checksum сохранённого part-файла."""
    try:
        if path.stat().st_size <= 0:
            return False
        manifest = _read_part_manifest(path)
        if manifest is None or manifest.get("version") != 1:
            return False
        if key is not None and manifest.get("key") != key:
            return False
        expected_size = int(manifest.get("size", -1))
        if expected_size != path.stat().st_size:
            return False
        expected_hash = str(manifest.get("sha256", "")).strip().lower()
        if not expected_hash:
            return False
        if _sha256_file(path) != expected_hash:
            return False
        return _parquet_ok(path)
    except Exception:  # noqa: BLE001
        return False


def _part_segments_path(path: Path) -> Path:
    return path.with_name(path.name + ".segments.json")


def _read_segments_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_part_segments_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    return payload


def _write_segments_manifest(
    path: Path,
    *,
    total_size: int,
    segment_size: int,
    done: set[int],
) -> None:
    target = _part_segments_path(path)
    tmp = target.with_name(target.name + ".tmp")
    payload = {
        "version": 1,
        "total_size": int(total_size),
        "segment_size": int(segment_size),
        "done": sorted(int(index) for index in done),
    }
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _ensure_part_file(part: Path, total_size: int) -> None:
    """Создаёт .part нужного размера, чтобы потоки могли писать по смещению."""
    if part.exists() and part.stat().st_size == total_size:
        return
    with part.open("wb") as fh:
        if total_size > 0:
            fh.seek(total_size - 1)
            fh.write(b"\0")


# ===== Загрузка одной части =====

def _apply_fresh_download(
    part: Path, data: bytes, total_size: int | None
) -> int:
    """Записывает тело HTTP 200 поверх part (без Range) и возвращает размер."""
    if total_size is not None and len(data) != total_size:
        raise OSError(
            f"Неполный HTTP 200 для {part.name}: "
            f"{len(data)} из {total_size} байт"
        )
    with part.open("wb") as fh:
        fh.write(data)
    return len(data)


def _apply_range_chunk(
    part: Path, start: int, data: bytes, chunk: int, total_size: int | None
) -> int:
    """Дописывает Range-чанк в part; проверяет, что ответ не выходит за конец."""
    if total_size is not None and start + len(data) > total_size:
        raise OSError(f"Range-ответ выходит за конец Overture part {part.name}")
    return _append_chunk(part, start, data, chunk)


def _download_complete(
    status: int, start: int, data_len: int, chunk: int, total_size: int | None
) -> bool:
    """Признак завершения загрузки part-файла."""
    if status != 206:
        return True
    if total_size is not None:
        return start == total_size
    return data_len < chunk


def _download_segment_range(
    urls: list[str],
    part: Path,
    start: int,
    length: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    expected_total: int,
    write_lock: threading.Lock,
) -> None:
    """Скачивает один сегмент [start, start + length) и пишет его в .part."""
    offset = 0
    while offset < length:
        status, data, chunk_total = _fetch_chunk(
            urls,
            start + offset,
            timeout,
            length - offset,
            retries,
            retry_delay,
        )
        if status != 206:
            raise _ParallelRangeFallback(
                f"Overture: сервер вернул HTTP {status} вместо 206 "
                f"для сегмента offset={start + offset}"
            )
        if chunk_total is not None and chunk_total != expected_total:
            raise OSError(
                f"Overture: размер файла изменился во время загрузки: "
                f"{chunk_total} != {expected_total}"
            )
        if not data:
            raise OSError(
                f"Overture: пустой Range-ответ для сегмента offset={start + offset}"
            )
        remaining = length - offset
        if len(data) > remaining:
            data = data[:remaining]
        with write_lock:
            with part.open("r+b") as fh:
                fh.seek(start + offset)
                fh.write(data)
        offset += len(data)


def _download_part_parallel(
    urls: list[str],
    part: Path,
    total_size: int,
    timeout: float,
    *,
    segment_size: int,
    workers: int,
    retries: int,
    retry_delay: float,
) -> int:
    """Параллельно докачивает .part несколькими Range-сегментами."""
    import concurrent.futures

    if total_size <= 0:
        _ensure_part_file(part, 0)
        return 0

    segment_size = max(1, int(segment_size))
    seg_path = _part_segments_path(part)
    manifest = _read_segments_manifest(part)

    done: set[int] = set()

    if (
        manifest is not None
        and int(manifest.get("total_size", -1)) == total_size
        and int(manifest.get("segment_size", -1)) == segment_size
    ):
        done = {int(index) for index in manifest.get("done", [])}
    else:
        with contextlib.suppress(OSError):
            seg_path.unlink()
        _ensure_part_file(part, total_size)
        _write_segments_manifest(
            part,
            total_size=total_size,
            segment_size=segment_size,
            done=done,
        )

    n_segments = (total_size + segment_size - 1) // segment_size
    pending = [index for index in range(n_segments) if index not in done]

    if pending:
        write_lock = threading.Lock()
        max_workers = min(max(1, workers), len(pending))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for index in pending:
                seg_start = index * segment_size
                seg_len = min(segment_size, total_size - seg_start)
                futures[
                    executor.submit(
                        _download_segment_range,
                        urls,
                        part,
                        seg_start,
                        seg_len,
                        timeout,
                        retries,
                        retry_delay,
                        total_size,
                        write_lock,
                    )
                ] = index

            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                future.result()
                done.add(index)
                _write_segments_manifest(
                    part,
                    total_size=total_size,
                    segment_size=segment_size,
                    done=done,
                )

    actual_size = part.stat().st_size
    if actual_size != total_size:
        raise OSError(
            f"Неполная параллельная Overture part: {actual_size} из {total_size} байт"
        )

    with contextlib.suppress(OSError):
        seg_path.unlink()

    return total_size


def _download_part_from_host_sequential(
    urls: str | list[str],
    part: Path,
    timeout: float,
    *,
    chunk: int = _OVERTURE_CHUNK_BYTES,
    per_chunk_retries: int = 3,
    retry_delay: float = _CHUNK_RETRY_DELAY_S,
) -> int:
    """Докачивает объект в part чанками последовательно и возвращает полный размер."""
    list_urls = [urls] if isinstance(urls, str) else list(urls)
    if not list_urls:
        raise ValueError("Overture: не переданы HTTP-хосты")

    start = part.stat().st_size if part.exists() else 0
    progress = _PartProgress(part.name, initial_bytes=start)
    total_size: int | None = None

    while True:
        status, data, chunk_total = _fetch_chunk(
            list_urls, start, timeout, chunk, per_chunk_retries, retry_delay
        )
        if chunk_total is not None:
            total_size = chunk_total
            progress.set_total(total_size)
            if total_size < start:
                raise OSError(
                    f"Некорректный размер Overture part: {total_size} < {start}"
                )
        if status != 206:
            start = _apply_fresh_download(part, data, total_size)
        else:
            start = _apply_range_chunk(part, start, data, chunk, total_size)

        progress.update(data)

        if _download_complete(status, start, len(data), chunk, total_size):
            if total_size is not None and start != total_size:
                raise OSError(
                    f"Неполная Overture part: {start} из {total_size} байт"
                )
            logger.info("Overture: %s", progress.summary())
            return start


def _download_part_from_host(
    urls: str | list[str],
    part: Path,
    timeout: float,
    *,
    chunk: int = _OVERTURE_CHUNK_BYTES,
    per_chunk_retries: int = 3,
    retry_delay: float = _CHUNK_RETRY_DELAY_S,
) -> int:
    """Докачивает объект в part. Сначала пытается параллельные Range-сегменты."""
    list_urls = [urls] if isinstance(urls, str) else list(urls)
    if not list_urls:
        raise ValueError("Overture: не переданы HTTP-хосты")

    seg_path = _part_segments_path(part)

    if part.exists() and not seg_path.exists():
        return _download_part_from_host_sequential(
            list_urls, part, timeout,
            chunk=chunk, per_chunk_retries=per_chunk_retries, retry_delay=retry_delay,
        )

    manifest = _read_segments_manifest(part)
    if manifest is not None:
        total_size = int(manifest.get("total_size", 0) or 0)
        segment_size = int(
            manifest.get("segment_size", _OVERTURE_SEGMENT_BYTES)
            or _OVERTURE_SEGMENT_BYTES
        )
        if total_size > 0 and segment_size > 0:
            try:
                return _download_part_parallel(
                    list_urls, part, total_size, timeout,
                    segment_size=segment_size, workers=_OVERTURE_SEGMENT_WORKERS,
                    retries=per_chunk_retries, retry_delay=retry_delay,
                )
            except _ParallelRangeFallback:
                logger.warning(
                    "Overture: параллельная докачка %s получила 200, "
                    "переключаемся на последовательную",
                    part.name,
                )
                with contextlib.suppress(OSError):
                    part.unlink()
                with contextlib.suppress(OSError):
                    seg_path.unlink()
                return _download_part_from_host_sequential(
                    list_urls, part, timeout,
                    chunk=chunk, per_chunk_retries=per_chunk_retries, retry_delay=retry_delay,
                )
        with contextlib.suppress(OSError):
            part.unlink()
        with contextlib.suppress(OSError):
            seg_path.unlink()

    status, probe, total_size = _fetch_chunk(
        list_urls, 0, timeout, 1, per_chunk_retries, retry_delay,
    )

    if status != 206:
        return _apply_fresh_download(part, probe, total_size)

    if total_size is None or total_size <= 0:
        return _download_part_from_host_sequential(
            list_urls, part, timeout,
            chunk=chunk, per_chunk_retries=per_chunk_retries, retry_delay=retry_delay,
        )

    if _OVERTURE_SEGMENT_WORKERS <= 1 or total_size <= _OVERTURE_SEGMENT_BYTES:
        return _download_part_from_host_sequential(
            list_urls, part, timeout,
            chunk=chunk, per_chunk_retries=per_chunk_retries, retry_delay=retry_delay,
        )

    try:
        return _download_part_parallel(
            list_urls, part, total_size, timeout,
            segment_size=_OVERTURE_SEGMENT_BYTES, workers=_OVERTURE_SEGMENT_WORKERS,
            retries=per_chunk_retries, retry_delay=retry_delay,
        )
    except _ParallelRangeFallback:
        logger.warning(
            "Overture: параллельная загрузка %s невозможна, "
            "используем последовательную",
            part.name,
        )
        with contextlib.suppress(OSError):
            part.unlink()
        with contextlib.suppress(OSError):
            seg_path.unlink()
        return _download_part_from_host_sequential(
            list_urls, part, timeout,
            chunk=chunk, per_chunk_retries=per_chunk_retries, retry_delay=retry_delay,
        )


def _finalize_stale_part(key: str, dest: Path, part: Path) -> bool:
    """Публикует `.part` после прошлого запуска, если он уже валидный parquet."""
    if not part.exists() or not _parquet_ok(part):
        return False
    size = part.stat().st_size
    sha256 = _sha256_file(part)
    with contextlib.suppress(OSError):
        _part_segments_path(part).unlink()
    part.replace(dest)
    _write_part_manifest(dest, key=key, size=size, sha256=sha256)
    return True


def _download_part_once(key: str, cache_dir: str | Path) -> None:
    """Скачивает одну часть атомарно и валидирует локальный cache-entry."""
    dest = _part_local_path(key, cache_dir)
    if dest.exists() and _is_valid_cached_part(dest, key=key):
        return

    if dest.exists():
        logger.warning("Overture: невалидный part-кэш, перекачиваем: %s", dest)
        with contextlib.suppress(OSError):
            dest.unlink()
        with contextlib.suppress(OSError):
            _part_manifest_path(dest).unlink()

    bucket, _, obj_path = key.partition("/")
    part = dest.with_name(dest.name + ".part")

    if _finalize_stale_part(key, dest, part):
        return

    urls = [
        _overture_host_url(host, bucket, key, obj_path)
        for host in _OVERTURE_HTTP_HOSTS
    ]

    _download_part_from_host(urls, part, timeout=120.0)

    if not _parquet_ok(part):
        with contextlib.suppress(OSError):
            part.unlink()
        with contextlib.suppress(OSError):
            _part_segments_path(part).unlink()
        raise OSError(f"Невалидный скачанный Overture part {key!r}")

    size = part.stat().st_size
    sha256 = _sha256_file(part)

    with contextlib.suppress(OSError):
        _part_segments_path(part).unlink()

    part.replace(dest)
    _write_part_manifest(dest, key=key, size=size, sha256=sha256)


# ===== STAC-резолвинг =====

def _retry_after_value(retry_after: str) -> float | None:
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None


def _retry_after_date(retry_after: str) -> float | None:
    try:
        when = email.utils.parsedate_to_datetime(retry_after)
        return (
            when - datetime_module.datetime.now(datetime_module.UTC)
        ).total_seconds()
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(retry_after: str | None, fallback: float) -> float:
    if not retry_after:
        return max(fallback, 30.0)
    seconds = _retry_after_value(retry_after)
    if seconds is not None:
        return max(seconds, 30.0)
    seconds = _retry_after_date(retry_after)
    if seconds is not None:
        return max(seconds, 30.0)
    return max(fallback, 30.0)


def _stac_retry_delay(exc: Exception, attempt: int, retry_delay: float) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        return _retry_after_seconds(retry_after, retry_delay * (2**attempt))
    return retry_delay * (2**attempt)


def _log_stac_retry(
    url: str,
    attempt: int,
    retries: int,
    delay: float,
    exc: Exception,
    rate_limited: bool,
) -> None:
    label = " (429)" if rate_limited else ""
    logger.warning(
        "Overture: STAC %s попытка %d/%d — повтор через %.1fs%s: %s",
        url,
        attempt + 1,
        retries + 1,
        delay,
        label,
        exc,
    )


def _read_stac_chunk(
    url: str | list[str] | tuple[str, ...],
    start: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    progress: _PartProgress,
) -> tuple[int, bytes, int | None]:
    status, data, total = _fetch_chunk(
        url,
        start=start,
        timeout=timeout,
        chunk=_STAC_CHUNK_BYTES,
        retries=retries,
        retry_delay=retry_delay,
    )
    if not data and status == 206:
        raise OSError(f"Пустой Range-ответ STAC для offset {start}")
    progress.set_total(total)
    if data:
        progress.update(data)
    return status, data, total


def _http_get_stac(
    url: str | list[str] | tuple[str, ...],
    timeout: float = _STAC_TIMEOUT_S,
    retries: int = 0,
    retry_delay: float = 2.0,
) -> bytes:
    """Скачивает STAC `collections.parquet` короткими HTTP Range-запросами."""
    display_url = url[0] if isinstance(url, (list, tuple)) else url
    progress = _PartProgress(display_url.rsplit("/", 1)[-1] or "STAC")
    chunks: list[bytes] = []
    start = 0
    total_size: int | None = None

    while True:
        status, data, reported_total = _read_stac_chunk(
            url, start, timeout, retries, retry_delay, progress
        )
        if status != 206:
            if start == 0:
                logger.info("Overture: STAC %s", progress.summary())
                return data
            raise OSError(
                "Overture STAC перестал поддерживать Range после частичного "
                f"чтения ({status} для offset {start})"
            )
        chunks.append(data)
        total_size = reported_total or total_size
        start += len(data)
        if total_size is not None:
            if start > total_size:
                raise OSError(
                    f"STAC Range превысил размер файла: "
                    f"{start} из {total_size} байт"
                )
            if start == total_size:
                logger.info("Overture: STAC %s", progress.summary())
                return b"".join(chunks)
        elif len(data) < _STAC_CHUNK_BYTES:
            logger.info("Overture: STAC %s", progress.summary())
            return b"".join(chunks)


def _resolve_stac_item_href(href: str, base_url: str) -> str:
    normalized = href.lstrip("./").removeprefix("collection.json/")
    parts = normalized.split("/", 1)
    if len(parts) == 2:
        first = parts[0]
        if len(first) >= 10 and first[4] == "-" and first[7] == "-" and "." in first:
            base_parts = urllib.parse.urlsplit(base_url)
            marker = "/" + first + "/"
            if marker in base_parts.path:
                # Ссылка уже содержит релиз, совпадающий с релизом базы.
                # Разрешать её надо относительно корня сервера, а не
                # относительно каталога релиза: иначе релиз дублируется
                # (``…/2026-08-19.0/2026-08-19.0/transportation/…``).
                root_url = urllib.parse.urlunsplit(
                    (base_parts.scheme, base_parts.netloc, "/", "", "")
                )
                return urllib.parse.urljoin(root_url, normalized)
    collection_dir = base_url.rsplit("/", 1)[0] + "/"
    return urllib.parse.urljoin(collection_dir, normalized)


def _bbox_intersects(
    item_bbox: Any, bbox: tuple[float, float, float, float]
) -> bool:
    if not item_bbox or len(item_bbox) != 4:
        return False
    min_lat, min_lon, max_lat, max_lon = bbox
    xmin, ymin, xmax, ymax = map(float, item_bbox)
    return xmin < max_lon and xmax > min_lon and ymin < max_lat and ymax > min_lat


def _stac_item_hrefs(
    collection: dict[str, Any],
    bbox: tuple[float, float, float, float],
    base_url: str,
) -> list[str]:
    """Возвращает href'ы STAC Item, чьи bbox пересекают заданный bbox."""
    item_links = [
        link.get("href")
        for link in collection.get("links", [])
        if link.get("rel") == "item" and link.get("href")
    ]
    extent_boxes = (
        collection.get("extent", {}).get("spatial", {}).get("bbox", [])
    )
    if len(extent_boxes) != len(item_links) + 1:
        return [_resolve_stac_item_href(href, base_url) for href in item_links]
    return [
        _resolve_stac_item_href(href, base_url)
        for href, item_bbox in zip(item_links, extent_boxes[1:])
        if _bbox_intersects(item_bbox, bbox)
    ]


def _s3_key_from_stac_item(item: dict[str, Any]) -> str | None:
    """Извлекает канонический S3 object key из STAC Item assets."""
    assets = item.get("assets", {})
    aws = assets.get("aws", {})
    alternate = aws.get("alternate", {})
    s3 = alternate.get("s3", {})
    href = s3.get("href")
    if not href:
        href = aws.get("href")
    if not isinstance(href, str):
        return None
    if href.startswith("s3://"):
        return href[5:]
    return None


def _stac_item_urls(item_url: str, release: str) -> list[str]:
    """Зеркальные URL STAC Item'а на всех `_STAC_HTTP_HOSTS`."""
    item_path = urllib.parse.urlsplit(item_url).path
    marker = f"/{release}/"
    if marker not in item_path:
        raise ValueError(f"Некорректный STAC Item URL: {item_url}")
    relative_path = item_path.rsplit(marker, 1)[1]
    relative_path = relative_path.replace("collection.json/", "", 1)
    return [
        f"{host}/{release}/{relative_path.lstrip('/')}"
        for host in _STAC_HTTP_HOSTS
    ]


def _fetch_stac_item_s3_key(
    item_url: str, release: str, retries: int, retry_delay: float
) -> str | None:
    item_data = _http_get_stac(
        _stac_item_urls(item_url, release),
        timeout=_STAC_META_TIMEOUT_S,
        retries=retries,
        retry_delay=retry_delay,
    )
    return _s3_key_from_stac_item(json.loads(item_data))


def _http_resolve_stac_part_files_via_collection(
    release: str,
    theme: str,
    overture_type: str,
    bbox: tuple[float, float, float, float],
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Основной STAC-резолвер через collection.json и Item JSON."""
    import concurrent.futures
    collection_urls = [
        f"{host}/{release}/{theme}/{overture_type}/collection.json"
        for host in _STAC_HTTP_HOSTS
    ]
    collection_data = _http_get_stac(
        collection_urls,
        timeout=_STAC_META_TIMEOUT_S,
        retries=retries,
        retry_delay=retry_delay,
    )
    collection = json.loads(collection_data)
    item_hrefs = _stac_item_hrefs(collection, bbox, collection_urls[0])
    if not item_hrefs:
        return []

    if _STAC_ITEM_JSON_LIMIT > 0 and len(item_hrefs) > _STAC_ITEM_JSON_LIMIT:
        raise RuntimeError(
            f"STAC: слишком много item'ов ({len(item_hrefs)}), "
            "переключаюсь на collections.parquet"
        )

    def fetch_item(item_url: str) -> str | None:
        return _fetch_stac_item_s3_key(item_url, release, retries, retry_delay)

    max_workers = min(_STAC_ITEM_WORKERS, len(item_hrefs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        return [key for key in executor.map(fetch_item, item_hrefs) if key]


def _stac_resolve_via_parquet(
    release: str,
    overture_type: str,
    bbox: tuple[float, float, float, float],
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Fallback-резолвер: вытащить ключи через `collections.parquet` STAC."""
    import pyarrow.compute as pc
    from pyarrow import parquet as pq
    stac_urls = [
        f"{host}/{release}/collections.parquet" for host in _STAC_HTTP_HOSTS
    ]
    data = _http_get_stac(
        stac_urls,
        timeout=_STAC_TIMEOUT_S,
        retries=retries,
        retry_delay=retry_delay,
    )
    table = pq.read_table(io.BytesIO(data))
    feature_type_filter = (pc.field("collection") == overture_type) & (
        pc.field("type") == "Feature"
    )
    min_lat, min_lon, max_lat, max_lon = bbox
    bbox_filter = (
        (pc.field("bbox", "xmin") < max_lon)
        & (pc.field("bbox", "xmax") > min_lon)
        & (pc.field("bbox", "ymin") < max_lat)
        & (pc.field("bbox", "ymax") > min_lat)
    )
    table = table.filter(feature_type_filter & bbox_filter)
    keys: list[str] = []
    for path in table.column("assets").to_pylist():
        href = path["aws"]["alternate"]["s3"]["href"]
        if href.startswith("s3://"):
            keys.append(href[len("s3://") :])
    return keys


def _http_resolve_stac_part_files(
    release: str,
    theme: str,
    overture_type: str,
    bbox: tuple[float, float, float, float],
    retries: int = 0,
    retry_delay: float = 2.0,
) -> list[str]:
    """Возвращает ключи частей через STAC."""
    try:
        return _http_resolve_stac_part_files_via_collection(
            release, theme, overture_type, bbox, retries, retry_delay
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Overture: STAC collection.json недоступен (%s); "
            "переключаемся на collections.parquet",
            exc,
        )
    try:
        return _stac_resolve_via_parquet(
            release, overture_type, bbox, retries, retry_delay
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Overture: STAC не удалось разрешить: %s", exc)
        return []


# ===== DuckDB fallback =====

def _duckdb_configure_provider(
    conn: Any,
    provider: str,
    release: str,
    theme: str,
    overture_type: str,
) -> str:
    """Настраивает DuckDB-расширения/секреты и возвращает источник паркетов."""
    if provider == "s3":
        conn.execute("LOAD httpfs")
        conn.execute("SET s3_region='us-west-2'")
        conn.execute("CREATE SECRET overture_s3 (TYPE s3, REGION 'us-west-2')")
        return (
            f"s3://overturemaps-us-west-2/release/{release}/"
            f"theme={theme}/type={overture_type}/*"
        )
    if provider == "azure":
        conn.execute("LOAD azure")
        conn.execute("SET azure_transport_option_type='curl'")
        conn.execute(
            "CREATE SECRET overture_azure (TYPE azure, PROVIDER config, "
            "ACCOUNT_NAME 'overturemapswestus2')"
        )
        return (
            f"az://overturemapswestus2.blob.core.windows.net/release/{release}/"
            f"theme={theme}/type={overture_type}/*"
        )
    raise ValueError(f"Неизвестный DuckDB provider: {provider!r}")


def _duckdb_bbox_query(
    source: str, target: Path, bbox: tuple[float, float, float, float]
) -> str:
    min_lat, min_lon, max_lat, max_lon = bbox
    return (
        "COPY ("
        " SELECT *"
        f" FROM read_parquet({_sql_literal(source)}, "
        "filename=true, hive_partitioning=1)"
        f" WHERE bbox.xmin < {max_lon}"
        f"   AND bbox.xmax > {min_lon}"
        f"   AND bbox.ymin < {max_lat}"
        f"   AND bbox.ymax > {min_lat}"
        f") TO {_sql_literal(str(target))} (FORMAT PARQUET)"
    )


def _duckdb_read_overture(
    release: str,
    theme: str,
    overture_type: str,
    bbox: tuple[float, float, float, float],
    *,
    provider: str = "s3",
) -> Any | None:
    """Извлекает bbox-подмножество Overture через DuckDB cloud scan."""
    import uuid
    import duckdb
    import geopandas as gpd

    target = Path.cwd() / f".overture_duckdb_{uuid.uuid4().hex}.parquet"
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("LOAD spatial")
        source = _duckdb_configure_provider(
            conn, provider, release, theme, overture_type
        )
        query = _duckdb_bbox_query(source, target, bbox)
        logger.info(
            "Overture: DuckDB %s/%s/%s → %s",
            theme,
            overture_type,
            provider,
            source,
        )
        started = time.monotonic()
        conn.execute(query)
        elapsed = max(time.monotonic() - started, 1e-9)
        if not target.exists() or target.stat().st_size <= 0:
            return None
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info(
            "Overture: DuckDB %s/%s: %.1f МБ за %.1f с (%.2f МБ/с)",
            theme,
            overture_type,
            size_mb,
            elapsed,
            size_mb / elapsed,
        )
        gdf = gpd.read_parquet(target)
        if "geometry" in gdf.columns:
            gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
        return gdf if len(gdf) > 0 else None
    finally:
        conn.close()
        with contextlib.suppress(OSError):
            target.unlink()


# ===== Локальный кэш частей =====

def _part_local_path(key: str, cache_dir: str | Path) -> Path:
    safe = key.replace("/", "__").replace("=", "")
    return Path(cache_dir) / "parts" / safe


def _release_from_part_name(name: str) -> str | None:
    """Извлекает release из имени локальной part-файла/компаньона.

    Имя образовано из object key заменой ``/`` → ``__`` и удалением ``=``,
    поэтому release идёт за маркером ``__release__`` (например,
    ``overturemaps-us-west-2__release__2026-08-19.0__...``).
    """
    marker = "__release__"
    idx = name.find(marker)
    if idx == -1:
        return None
    release = name[idx + len(marker):].split("__", 1)[0]
    return release or None


def _release_from_key(key: str) -> str | None:
    """Извлекает release из S3 object key вида ``.../release/<release>/...``."""
    marker = "/release/"
    idx = key.find(marker)
    if idx == -1:
        return None
    release = key[idx + len(marker):].split("/", 1)[0]
    return release or None


def _prune_other_release_parts(cache_dir: str | Path, current_release: str) -> int:
    """Удаляет part-файлы релизов, отличных от текущего.

    Новый release Overture перезаписывает старые: после скачивания частей
    текущего release из кэша исключаются parts всех предыдущих. Удаляются и
    base-файлы, и их компаньоны (``.part``/``.meta.json``/``.segments.json``).
    Возвращает число удалённых файлов.
    """
    if not current_release:
        return 0
    parts_dir = Path(cache_dir) / "parts"
    if not parts_dir.is_dir():
        return 0

    current = current_release.strip().lower()
    removed = 0
    for path in parts_dir.iterdir():
        if not path.is_file():
            continue
        release = _release_from_part_name(path.name)
        if release is None or release.lower() == current:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info(
            "Overture: удалено %d parts старых регионов (current release=%s)",
            removed,
            current_release,
        )
    return removed


def _download_part_round(keys: list[str], cache_dir: str | Path) -> None:
    """Одна попытка скачать все части параллельно (ThreadPoolExecutor)."""
    import concurrent.futures

    def _download_one(key: str) -> None:
        _download_part_once(key, cache_dir)

    max_workers = min(_OVERTURE_PART_WORKERS, max(1, len(keys)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download_one, k): k for k in keys}
        for future in concurrent.futures.as_completed(futures):
            future.result()


def _download_overture_parts(
    keys: list[str], cache_dir: str | Path, retries: int, retry_delay: float
) -> None:
    """Качает отсутствующие части по HTTP, повторяя неудавшиеся попытки.

    После успешной загрузки старого release на диске перезаписываются новым:
    parts всех прочих релизов удаляются из кэша (см. ``_prune_other_release_parts``).
    """
    parts_dir = Path(cache_dir) / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    current_release = next(
        (rel for key in keys if (rel := _release_from_key(key))), ""
    )
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            _download_part_round(keys, cache_dir)
            _prune_other_release_parts(cache_dir, current_release)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if attempt < retries:
            delay = retry_delay * (2**attempt)
            logger.warning(
                "Overture: скачивание частей попытка %d/%d — "
                "повтор через %.1fs: %s",
                attempt + 1,
                retries + 1,
                delay,
                last_exc,
            )
            time.sleep(delay)
    raise last_exc if last_exc is not None else RuntimeError(
        "Overture: скачивание частей не удалось"
    )


# ===== Чтение локальных частей =====

def _read_part_frames(
    local_files: list[str], bbox_filter: tuple, gpd: Any
) -> list[Any]:
    """Читает локальные parquet-части с bbox-фильтром параллельно."""
    import concurrent.futures

    def _read_one(path: str) -> Any:
        try:
            frame = gpd.read_parquet(path, bbox=bbox_filter)
        except (ValueError, TypeError):
            frame = gpd.read_parquet(path)
            frame = frame.cx[
                bbox_filter[0] : bbox_filter[2],
                bbox_filter[1] : bbox_filter[3],
            ]
        return frame

    if len(local_files) <= 1:
        return [_read_one(local_files[0])] if local_files else []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_OVERTURE_READ_WORKERS, len(local_files))
    ) as executor:
        return list(executor.map(_read_one, local_files))


def _concat_part_frames(
    frames: list[Any], gpd: Any, *, to_epsg4326: bool = True
) -> Any:
    """Склеивает кадры частей, приводя CRS к EPSG:4326 при необходимости."""
    if not frames:
        return gpd.pd.DataFrame()
    gdf = gpd.pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if gdf.crs is None:
        return gdf.set_crs("EPSG:4326")
    if to_epsg4326 and str(gdf.crs).upper() != "EPSG:4326":
        return gdf.to_crs("EPSG:4326")
    return gdf


def _read_overture_parts(
    local_files: list[str], bbox_filter: tuple, gpd: Any
) -> Any | None:
    frames = _read_part_frames(local_files, bbox_filter, gpd)
    if not frames:
        return None
    gdf = _concat_part_frames(frames, gpd)
    return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]


__all__ = [
    "_CHUNK_RETRY_DELAY_S",
    "_OVERTURE_CHUNK_BYTES",
    "_OVERTURE_HTTP_HOSTS",
    "_OVERTURE_SEGMENT_BYTES",
    "_OVERTURE_SEGMENT_WORKERS",
    "_OVERTURE_PART_WORKERS",
    "_OVERTURE_READ_WORKERS",
    "_STAC_CHUNK_BYTES",
    "_STAC_HTTP_HOSTS",
    "_STAC_ITEM_WORKERS",
    "_STAC_ITEM_JSON_LIMIT",
    "_STAC_META_TIMEOUT_S",
    "_STAC_TIMEOUT_S",
    "_PartProgress",
    "_ParallelRangeFallback",
    "_append_chunk",
    "_concat_part_frames",
    "_download_overture_parts",
    "_download_part_from_host",
    "_download_part_from_host_sequential",
    "_download_part_parallel",
    "_download_part_once",
    "_download_part_round",
    "_duckdb_read_overture",
    "_fetch_chunk",
    "_http_get_range",
    "_http_get_stac",
    "_http_get_url",
    "_http_resolve_stac_part_files",
    "_is_valid_cached_part",
    "_log_stac_retry",
    "_overture_host_url",
    "_part_local_path",
    "_prune_other_release_parts",
    "_read_overture_parts",
    "_read_part_frames",
    "_release_from_key",
    "_release_from_part_name",
    "_retry_after_date",
    "_retry_after_seconds",
    "_retry_after_value",
    "_stac_retry_delay",
]
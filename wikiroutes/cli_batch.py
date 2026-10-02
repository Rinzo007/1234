"""Пакетный режим CLI.

Вынесен из основного CLI-runtime, чтобы пакетное выполнение команд не
смешивалось с интерактивным запуском и экспортом результатов.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

# Суффикс файла состояния пакета (рядом с batch-файлом).
_STATE_SUFFIX = ".state.json"


def _read_batch_text(batch_file: str) -> str:
    """Читает файл команд: utf-8-sig с fallback на cp1251."""
    path = Path(batch_file)
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise ValueError(f"Не удалось открыть batch-файл {batch_file}: {exc}") from exc
    raise ValueError(
        f"Не удалось прочитать batch-файл {batch_file}: невалидная кодировка"
    )


def _clean_command_tokens(tokens: list[str]) -> list[str]:
    """Убирает префикс запуска (python -m wikiroutes / wikiroutes) и --batch."""
    if tokens[0] in ("python", "py", "python3") and tokens[1:3] == [
        "-m",
        "wikiroutes",
    ]:
        tokens = tokens[3:]
    elif tokens[0] == "wikiroutes":
        tokens = tokens[1:]

    cleaned: list[str] = []
    skip_next = False
    for item in tokens:
        if skip_next:
            skip_next = False
            continue
        if item == "--batch":
            skip_next = True
            continue
        if item.startswith("--batch="):
            continue
        cleaned.append(item)
    return cleaned


def _split_stripped_line(stripped: str) -> list[str] | None:
    """Разбивает строку команды на токены; None при синтаксической ошибке."""
    if " #" in stripped:
        stripped = stripped.split(" #", 1)[0].strip()
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        print(f"  ⚠ строка пропущена (ошибка разбора: {exc}): {stripped}")
        return None
    return tokens if tokens else None


def parse_batch_file(batch_file: str) -> list[tuple[str, list[str]]]:
    """Читает файл команд и возвращает ``(сырая строка, аргументы)``.

    Бросает ``ValueError`` с понятным сообщением, если файл не читается;
    строки с синтаксической ошибкой (незакрытая кавычка) пропускаются
    с предупреждением.
    """
    text = _read_batch_text(batch_file)

    commands: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, ...]] = set()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue

        tokens = _split_stripped_line(stripped)
        if tokens is None:
            continue

        cleaned = _clean_command_tokens(tokens)
        if not cleaned:
            continue

        key = tuple(cleaned)
        if key in seen:
            print(f"  ⚠ дубль команды пропущен: {' '.join(cleaned)}")
            continue
        seen.add(key)
        commands.append((stripped, cleaned))

    return commands


def _command_key(tokens: list[str]) -> str:
    """Стабильный ключ команды для файла состояния (без учёта порядка строк)."""
    return "\x1f".join(tokens)


def _default_state_path(batch_file: str) -> Path:
    return Path(batch_file).with_suffix(_STATE_SUFFIX)


def _load_state(state_path: Path) -> dict:
    """Читает состояние пакета; при повреждении возвращает пустое."""
    if not state_path.exists():
        return {"commands": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"commands": {}}
    if not isinstance(data, dict) or not isinstance(data.get("commands"), dict):
        return {"commands": {}}
    return data


def _save_state(state_path: Path, state: dict) -> None:
    """Атомарно сохраняет состояние пакета (temp + rename)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(state_path)


def _reconfigure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError):
                reconfigure(encoding="utf-8", errors="replace")


class _BatchState:
    """Общее состояние выполнения пакета (данные и служебные поля)."""
    __slots__ = ("done", "failures", "reset", "state_file")

    def __init__(
        self,
        done: dict,
        state_file: Path,
        reset: bool,
        failures: list[str],
    ) -> None:
        self.done = done
        self.state_file = state_file
        self.reset = reset
        self.failures = failures


def _run_command(main: Callable[[list[str]], int], tokens: list[str]) -> int:
    """Исполняет команду; SystemExit и ошибки сводятся к коду возврата."""
    try:
        return main(tokens)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception:  # noqa: BLE001 — изоляция команд батча: traceback и переход к следующей
        traceback.print_exc()
        return 1


def _command_city(tokens: list[str]) -> str:
    """Первый токен-город команды (без опций) или пустая строка."""
    first = tokens[0] if tokens else ""
    return first if first and not first.startswith("-") else ""


def _already_done(ctx: _BatchState, key: str) -> bool:
    """Пропускается ли команда как уже выполненная (без reset)."""
    if ctx.reset:
        return False
    prev = ctx.done.get(key)
    return prev is not None and prev.get("status") == "done"


def _batch_iteration(
    index: int,
    total: int,
    raw_line: str,
    tokens: list[str],
    main: Callable[[list[str]], int],
    ctx: _BatchState,
) -> tuple[int, int]:
    """Выполняет одну команду пакета; возвращает (skipped, executed)."""
    key = _command_key(tokens)
    city = _command_city(tokens)
    if _already_done(ctx, key):
        tag = f" {city}" if city else ""
        print(f"[{index}/{total}] ⏭{tag} (уже выполнено)")
        return 1, 0

    label = f"[{index}/{total}] {city} " if city else f"[{index}/{total}]"
    print(f"{label}→ {' '.join(tokens)}")
    code = _run_command(main, tokens)

    status = "done" if code == 0 else "failed"
    ctx.done[key] = {"status": status, "code": code, "line": raw_line}
    _save_state(ctx.state_file, {"commands": ctx.done})

    if code:
        ctx.failures.append(f"  ✗ {raw_line} (код {code})")
    return 0, 1


def _print_batch_summary(
    done: dict,
    executed: int,
    skipped: int,
    failures: list[str],
    total: int,
) -> int:
    """Печатает итоги пакета; возвращает код возврата."""
    print("\n" + "═" * 60)
    if executed == 0 and skipped:
        print("Все команды уже выполнены — повторный запуск ничего не делает.")
    else:
        done_now = executed - len(failures)
        msg = f"Готово: {done_now}/{executed} команд выполнено за этот запуск"
        if skipped:
            msg += f" (+{skipped} пропущено как уже готовые)"
        print(msg + ".")
    total_done = sum(1 for v in done.values() if v.get("status") == "done")
    print(f"Всего выполнено в пакете: {total_done}/{total}.")
    if failures:
        print("Неудачные команды:")
        print("\n".join(failures))
    print("═" * 60)
    return 1 if failures else 0


def run_batch(
    batch_file: str,
    main: Callable[[list[str]], int],
    *,
    reset: bool = False,
    state_path: str | None = None,
) -> int:
    """Выполняет команды последовательно и помнит, какие уже готовы.

    Состояние сохраняется рядом с batch-файлом (``<file>.state.json``).
    При повторном запуске команды со статусом ``done`` пропускаются — это
    позволяет продолжить пакет после обрыва (ошибка/OOM/ручная остановка),
    не пересчитывая уже обработанные города.

    ``reset=True`` очищает сохранённое состояние и выполняет всё заново.
    """
    _reconfigure_streams()

    try:
        commands = parse_batch_file(batch_file)
    except ValueError as exc:
        print(f"⚠ {exc}")
        return 1

    if not commands:
        print(f"⚠ В файле {batch_file} не найдено ни одной команды.")
        return 1

    state_file = Path(state_path) if state_path else _default_state_path(batch_file)
    if reset:
        print(f"Сброс состояния пакета: {state_file}")
        state: dict = {"commands": {}}
    else:
        state = _load_state(state_file)
    done: dict = state.setdefault("commands", {})

    print(f"Пакет: {len(commands)} команд из {batch_file}")
    already_done = sum(1 for v in done.values() if v.get("status") == "done")
    if already_done:
        print(f"  уже выполнено ранее: {already_done} (будут пропущены)")
    print()

    ctx = _BatchState(done, state_file, reset, [])
    skipped = 0
    executed = 0

    for index, (raw_line, tokens) in enumerate(commands, start=1):
        skip, run = _batch_iteration(
            index, len(commands), raw_line, tokens, main, ctx
        )
        skipped += skip
        executed += run

    return _print_batch_summary(
        done, executed, skipped, ctx.failures, len(commands)
    )


__all__ = ["parse_batch_file", "run_batch"]

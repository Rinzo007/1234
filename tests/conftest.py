"""Общая настройка тестов: пути импорта и порядок загрузки пакетов.

Корень проекта — каталог ``wikiroutes``. Он добавляется в ``sys.path``,
потому что внутренние модули пакета импортируют друг друга плоскими
именами (``from cache import JsonCache``), как принято в этом проекте.
"""

from __future__ import annotations

from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "wikiroutes"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

# В окружении установлен сторонний пакет ``overture-schema-theme-addresses``,
# который тоже предоставляет модуль с именем ``overture``. Pytest регистрирует
# его как плагин **до** загрузки conftest, поэтому в sys.modules уже лежит
# чужой ``overture``, и проектный пакет из wikiroutes/ был бы недостижим.
#
# Решение: выгружаем всё дерево ``overture*`` из sys.modules и импортируем
# проектный пакет заново. Без этого `import overture` в тестах подхватывал
# чужой модуль, а ``overture.config`` не существовал вовсе.
_STALE_PREFIXES = ("overture", "overture_schema_theme_addresses")

for _name in [
    key for key in sys.modules if key.split(".")[0] in _STALE_PREFIXES
]:
    del sys.modules[_name]

import overture  # noqa: E402,F401  — фиксирует проектный пакет в sys.modules

assert Path(overture.__file__ or "").is_relative_to(PACKAGE_ROOT), (
    "в sys.modules попал посторонний пакет 'overture' вместо wikiroutes/overture: "
    f"{overture.__file__}"
)
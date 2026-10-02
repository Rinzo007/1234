"""HTTP-слой: requests-сессии с retry и таймаутами по умолчанию.

Для текущего синхронного приложения используется поддерживаемый ``requests``.
Миграция на ``httpx`` не требуется для корректности и вынесена в отдельное
архитектурное изменение.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import requests
from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest, Response
from urllib3.util import Retry

from .constants import HEADERS, MAX_RETRIES, MAX_WORKERS

logger = logging.getLogger("wikiroutes.http_client")

DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 120.0)

# Пул должен вмещать сессии всех воркеров, иначе urllib3 переполняется,
# роняет установленные соединения и повторно ходит TLS-хендшейками.
POOL_SIZE = max(16, MAX_WORKERS + 2)


class _TimeoutAdapter(HTTPAdapter):
    """Подставляет таймаут по умолчанию, если вызывающий его не задал."""

    def __init__(
        self,
        *,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        self._default_timeout = timeout
        super().__init__(**kwargs)

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | None = None,
        verify: bool | str = True,
        cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> Response:
        """Отправляет HTTP-запрос с безопасным таймаутом по умолчанию."""
        if timeout is None:
            timeout = self._default_timeout
        return super().send(
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )


def build_session() -> requests.Session:
    """Создаёт настроенную ``requests.Session`` с retry и TLS verification."""
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
    )
    adapter = _TimeoutAdapter(
        max_retries=retry,
        pool_connections=POOL_SIZE,
        pool_maxsize=POOL_SIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.verify = True
    session.headers.update(HEADERS)
    return session


class SessionProvider:
    """Предоставляет одну ``requests.Session`` на поток.

    Экземпляр также закрывает все созданные сессии через ``close()`` и
    поддерживает контекстный менеджер.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    def get(self) -> requests.Session:
        """Возвращает сессию, привязанную к текущему потоку."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = build_session()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        """Закрывает все созданные HTTP-сессии."""
        with self._lock:
            sessions = self._sessions
            self._sessions = []
        for session in sessions:
            try:
                session.close()
            except (OSError, RuntimeError):
                logger.debug("Не удалось закрыть HTTP-сессию", exc_info=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

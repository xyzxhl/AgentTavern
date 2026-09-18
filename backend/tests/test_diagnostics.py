"""不依赖开发者模式的服务诊断日志。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agenttavern.diagnostics import configure_service_logging


def test_service_log_is_persisted_and_rotatable(tmp_path: Path) -> None:
    path = configure_service_logging(tmp_path)
    logging.getLogger("agenttavern.test").error("diagnostic-marker")
    for handler in logging.getLogger("agenttavern").handlers:
        handler.flush()

    assert path == (tmp_path / "logs" / "service.log").resolve()
    assert "diagnostic-marker" in path.read_text(encoding="utf-8")
    rotating = next(
        handler
        for handler in logging.getLogger("agenttavern").handlers
        if isinstance(handler, RotatingFileHandler)
    )
    assert rotating.maxBytes == 5 * 1024 * 1024
    assert rotating.backupCount == 3

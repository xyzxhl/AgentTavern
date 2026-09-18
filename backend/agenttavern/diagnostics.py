"""服务级诊断日志。

trace 回答「某个 run 做过什么」，这里回答「为什么连 run_failed 都没来得及送到」。
只记生命周期与调用元数据，不记 prompt、正文或密钥；完整模型往返仍只在开发者 trace。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_HANDLER_MARK = "_agenttavern_service_log"


def configure_service_logging(data_dir: Path) -> Path:
    """把 ``agenttavern.*`` 日志写进有界轮转文件，并返回当前文件路径。"""
    path = (data_dir / "logs" / "service.log").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("agenttavern")
    logger.setLevel(logging.INFO)

    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARK, False):
            continue
        if Path(getattr(handler, "baseFilename", "")).resolve() == path:
            return path
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.info("service_log_ready path=%s", path)
    return path

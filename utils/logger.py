import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime


def setup_logger(name: str, log_dir: str = "logs", level: str = "INFO",
                 max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 콘솔 핸들러
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 파일 핸들러 (날짜별 로테이션)
    log_file = os.path.join(log_dir, f"{name}_{datetime.now().strftime('%Y%m%d')}.log")
    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger

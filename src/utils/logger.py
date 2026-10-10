import os
import logging
from logging.handlers import RotatingFileHandler


def setup_logger(config):
    log_dir = config.get('storage', {}).get('log_dir', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    formatter = logging.Formatter(log_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 控制台输出
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # 文件输出（滚动10MB x 5份）
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "pm_agent.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 第三方库日志降级
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return root_logger

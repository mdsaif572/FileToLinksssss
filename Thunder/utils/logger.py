# Thunder/utils/logger.py

import logging
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import os
import queue
import atexit
import sys

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'bot.txt')

logging._srcfile = None
logging.logThreads = 0
logging.logProcesses = 0 

log_queue = queue.Queue(maxsize=10000)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler(stream=sys.__stdout__)
console_handler.setFormatter(formatter)
console_handler.stream.reconfigure(encoding='utf-8', errors='replace')

listener = QueueListener(log_queue, file_handler, console_handler, respect_handler_level=True)
listener.start()

logger = logging.getLogger('ThunderBot')
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(QueueHandler(log_queue))

atexit.register(listener.stop)

def read_log_tail(path: str = LOG_FILE, max_bytes: int = 200 * 1024) -> bytes:
    """Read the last ``max_bytes`` of a log file, starting at a line boundary.

    The log rotates at 10 MB, so sending the whole file made /log slow (and the
    command's update could even be dropped while the dispatcher was busy).
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(size - max_bytes, 0)
        handle.seek(start)
        data = handle.read()

    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline != -1 else data
    return data


__all__ = ['logger', 'LOG_FILE', 'read_log_tail']

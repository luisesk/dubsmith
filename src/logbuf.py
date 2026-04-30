"""In-memory ring buffer log handler. Last N lines retained."""
import logging
import threading
from collections import deque
from datetime import datetime


class RingBuffer(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.buf: deque[str] = deque(maxlen=capacity)
        # NOTE: do NOT shadow `self.lock` from the base class — it's an RLock
        # used by handle()/acquire(). We use a separate buf_lock for buffer access.
        self.buf_lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            line = f"{ts} {record.levelname:<5} {record.name}: {record.getMessage()}"
            if record.exc_info:
                line += "\n" + self.format(record).split("\n", 1)[-1]
            with self.buf_lock:
                self.buf.append(line)
        except Exception:
            self.handleError(record)

    def tail(self, n: int = 200, level: str | None = None) -> list[str]:
        with self.buf_lock:
            lines = list(self.buf)
        if level:
            lines = [l for l in lines if f" {level.upper():<5} " in l]
        return lines[-n:]


_RING: RingBuffer | None = None


def install(capacity: int = 2000, level: int = logging.INFO) -> RingBuffer:
    global _RING
    if _RING is not None:
        return _RING
    _RING = RingBuffer(capacity)
    _RING.setLevel(level)
    logging.getLogger().addHandler(_RING)
    return _RING


def get() -> RingBuffer | None:
    return _RING

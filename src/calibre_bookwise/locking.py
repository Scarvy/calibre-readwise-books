"""Lock helpers for the sync queues, which the GUI and a poll thread share.

Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

from collections.abc import Generator
from contextlib import contextmanager
from threading import RLock


@contextmanager
def unlocked(lock: RLock) -> Generator[None]:
    """Release lock for the block, e.g. around a slow network request.

    The caller must hold lock exactly once (an RLock held twice stays held),
    and must re-check any state it read before the block, which another
    thread may have changed meanwhile.
    """
    lock.release()
    try:
        yield
    finally:
        lock.acquire()

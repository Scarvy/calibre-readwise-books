import threading

import pytest

from calibre_bookwise.locking import unlocked


def taken_by_another_thread(lock) -> bool:
    """Whether another thread can acquire lock right now (it releases it again)."""
    result = []

    def attempt():
        result.append(lock.acquire(blocking=False))
        if result[0]:
            lock.release()

    other = threading.Thread(target=attempt)
    other.start()
    other.join()
    return result[0]


def test_lock_is_free_inside_the_block_and_held_again_after():
    lock = threading.RLock()
    with lock:
        with unlocked(lock):
            assert taken_by_another_thread(lock)
        assert not taken_by_another_thread(lock)


def test_lock_is_held_again_when_the_block_raises():
    lock = threading.RLock()
    with lock:
        with pytest.raises(RuntimeError), unlocked(lock):
            raise RuntimeError
        assert not taken_by_another_thread(lock)

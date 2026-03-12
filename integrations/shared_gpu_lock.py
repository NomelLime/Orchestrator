"""
integrations/shared_gpu_lock.py — Cross-project GPU lock for Ollama/VRAM contention.

Orchestrator and ShortsProject both use Ollama on the same GPU (12GB VRAM).
This portalocker-based file lock prevents simultaneous LLM/VL inference that
would cause OOM errors or model eviction.

Lock file: C:\\Users\\lemon\\Documents\\GitHub\\.gpu_lock
(same path used by ShortsProject/pipeline/shared_gpu_lock.py)

Usage:
    from integrations.shared_gpu_lock import acquire_gpu_lock

    with acquire_gpu_lock(consumer="Orchestrator-Evolution", timeout=120):
        raw_response = call_llm(model, prompt)
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_GPU_LOCK_FILE = Path(r"C:\Users\lemon\Documents\GitHub\.gpu_lock")


@contextmanager
def acquire_gpu_lock(consumer: str = "unknown", timeout: float = 120.0):
    """
    Context manager that acquires the cross-project GPU file lock.
    Blocks until the lock is available or timeout is reached.
    On timeout: logs a warning and proceeds without the lock (graceful degradation).

    Args:
        consumer: label used in log messages (e.g. "Orchestrator-Evolution")
        timeout:  max seconds to wait for the lock before proceeding anyway
    """
    try:
        import portalocker
    except ImportError:
        logger.debug("[GPU-Lock] portalocker not installed — skipping lock for %s", consumer)
        yield
        return

    lock_file = None
    acquired = False
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            lock_file = open(str(_GPU_LOCK_FILE), "w")
            portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
            acquired = True
            break
        except portalocker.AlreadyLocked:
            if lock_file:
                try:
                    lock_file.close()
                except Exception:
                    pass
                lock_file = None
            time.sleep(2.0)
        except Exception as e:
            logger.warning("[GPU-Lock] Lock error for %s: %s", consumer, e)
            if lock_file:
                try:
                    lock_file.close()
                except Exception:
                    pass
                lock_file = None
            break

    if not acquired:
        logger.warning(
            "[GPU-Lock] Timeout (%ds) waiting for GPU lock (%s) — proceeding without lock",
            int(timeout), consumer,
        )

    try:
        yield
    finally:
        if acquired and lock_file:
            try:
                import portalocker as _pl
                _pl.unlock(lock_file)
                lock_file.close()
            except Exception:
                pass

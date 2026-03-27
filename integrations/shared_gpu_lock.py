"""
Cross-project GPU file lock — реэкспорт из пакета shared_gpu_lock.

Тот же API, что раньше: acquire_gpu_lock(consumer=..., timeout=...).
"""

from shared_gpu_lock.file_lock import acquire_gpu_lock, get_gpu_lock_file_path

__all__ = ["acquire_gpu_lock", "get_gpu_lock_file_path"]

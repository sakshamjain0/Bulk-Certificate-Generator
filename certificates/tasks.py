"""Scheduling of job processing.

The API only calls ``enqueue_job``. Swapping the in-process thread pool for
Celery/RQ means changing this module only (e.g. ``process_job_task.delay(id)``).
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections, connection, transaction

from .services import abort_job, process_job

logger = logging.getLogger(__name__)

_executor = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=settings.CERTIFICATE_WORKER_THREADS,
                thread_name_prefix="certificate-worker",
            )
        return _executor


def run_job_safely(job_id) -> None:
    """Run a job, turning any unexpected crash into a FAILED job status."""
    try:
        process_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s crashed", job_id)
        abort_job(job_id, f"{type(exc).__name__}: {exc}")


def _run_job_in_thread(job_id) -> None:
    # Worker threads get their own DB connection; make sure it is fresh and
    # closed afterwards so connections are not leaked.
    close_old_connections()
    try:
        run_job_safely(job_id)
    finally:
        connection.close()


def enqueue_job(job_id) -> None:
    mode = settings.CERTIFICATE_PROCESSING_MODE
    if mode == "sync":
        run_job_safely(job_id)
    elif mode == "thread":
        # Only start once the job rows are committed, otherwise the worker
        # might not see them.
        transaction.on_commit(lambda: _get_executor().submit(_run_job_in_thread, job_id))
    else:
        raise ValueError(f"Unknown CERTIFICATE_PROCESSING_MODE: {mode!r}")

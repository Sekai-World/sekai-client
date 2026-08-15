"""Gunicorn lifecycle hooks for the shared account client."""


def worker_exit(server, worker) -> None:
    """Release an active remote account lease during graceful worker exit."""
    del server, worker
    from shared_client import release_active_account_lease

    release_active_account_lease()

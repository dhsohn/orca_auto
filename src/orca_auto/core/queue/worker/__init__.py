"""The parent worker's generic queue loop.

``loop`` holds ``QueueWorkerLoop``, ``admission`` the capacity check and the
claimable-row selection, ``models`` the reservation records, and ``pid_file``
the worker PID file. Callers import the submodule they need.
"""

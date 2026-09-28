"""Durable disk queue, with modules named by the process that runs them.

``store`` (the only ``queue.json`` writer), ``persistence``, ``types``,
``transitions``, ``publication`` and the other row modules serve every process.
The parent worker runs ``worker`` and ``processes``; the worker child runs
``child`` and also uses ``processes`` to install its shutdown signal handlers
and stop the engine's process group. ``snapshot_intent`` and
``generation_owner`` serve every process that creates, claims or recovers a
generation. Consumers import the submodule they need; the package exposes only
the error type that the CLI layer catches without depending on the store module.
"""

from .persistence import QueueStoreCorruptError

__all__ = ["QueueStoreCorruptError"]

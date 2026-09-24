"""Durable disk queue: store, publication protocol, worker loop, and child execution.

Consumers import the submodule they need (``store``, ``transitions``,
``publication``, ``persistence``, ...). The package exposes only the error type that the CLI
layer catches without depending on the store module.
"""

from .persistence import QueueStoreCorruptError

__all__ = ["QueueStoreCorruptError"]

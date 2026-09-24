"""Exception types raised by the RAM scratch workspace machinery.

``EngineScratchError`` marks any scratch failure that needs inspection;
``EngineScratchCapacityError`` narrows it to a refusal that left nothing
behind, so a caller may wait and retry. Every other submodule builds on
this one and none of them is imported here.
"""

from __future__ import annotations


class EngineScratchError(RuntimeError):
    """Raised when a RAM scratch workspace cannot be used or published safely."""


class EngineScratchCapacityError(EngineScratchError):
    """Raised when the scratch root is safe but cannot admit a workspace right now.

    Only `EngineScratchWorkspace.create` raises it, and only with no workspace
    left behind, so a caller that has not started its engine may wait and ask
    again. Every other scratch failure needs inspection and stays the base type.
    """

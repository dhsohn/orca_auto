"""Shared namespace package for the chemistry automation monorepo."""

from pkgutil import extend_path

# Core owns this initializer; separately installed extensions own subpackages.
# Never discover sibling checkout sources unless they were explicitly installed.
__path__ = extend_path(__path__, __name__)

from ._version import __version__

__all__ = ["__version__"]

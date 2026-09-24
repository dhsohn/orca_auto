"""Server-owned molecule admission limits shared by every execution engine."""

MAX_ADMISSION_ATOMS = 10_000
MAX_HESSIAN_ADMISSION_ATOMS = 1_000

__all__ = [
    "MAX_ADMISSION_ATOMS",
    "MAX_HESSIAN_ADMISSION_ATOMS",
]

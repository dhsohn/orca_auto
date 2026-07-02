from __future__ import annotations

from typing import Any

_ALLOWED_ORDERS = {
    "mtime DESC",
    "mtime ASC",
    "energy_hartree ASC",
    "energy_hartree DESC",
    "indexed_at DESC",
    "formula ASC",
}


def build_dft_query(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    for col in ("method", "basis_set", "calc_type", "status", "formula"):
        if value := filters.get(col):
            conditions.append(f"{col} = ?")
            params.append(value)

    if "method_like" in filters:
        conditions.append("method LIKE ?")
        params.append(f"%{filters['method_like']}%")

    if "formula_like" in filters:
        conditions.append("formula LIKE ?")
        params.append(f"%{filters['formula_like']}%")

    if "energy_min" in filters:
        conditions.append("energy_hartree >= ?")
        params.append(filters["energy_min"])
    if "energy_max" in filters:
        conditions.append("energy_hartree <= ?")
        params.append(filters["energy_max"])

    if "opt_converged" in filters:
        conditions.append("opt_converged = ?")
        params.append(1 if filters["opt_converged"] else 0)

    if "has_imaginary_freq" in filters:
        conditions.append("has_imaginary_freq = ?")
        params.append(1 if filters["has_imaginary_freq"] else 0)

    where = " AND ".join(conditions) if conditions else "1=1"
    limit = min(int(filters.get("limit", 50)), 200)
    order = filters.get("order_by", "mtime DESC")
    if order not in _ALLOWED_ORDERS:
        order = "mtime DESC"

    sql = f"SELECT * FROM dft_calculations WHERE {where} ORDER BY {order} LIMIT ?"
    params.append(limit)
    return sql, params


def lowest_energy_filters(formula: str | None = None, limit: int = 5) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "order_by": "energy_hartree ASC",
        "limit": limit,
    }
    if formula:
        filters["formula"] = formula
    return filters


def comparison_filters(
    *,
    formula: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "order_by": "energy_hartree ASC",
        "limit": 50,
    }
    if formula:
        filters["formula"] = formula
    if method:
        filters["method"] = method
    return filters

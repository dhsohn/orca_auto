from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS dft_calculations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path        TEXT    NOT NULL UNIQUE,
    file_hash          TEXT    NOT NULL,
    mtime              REAL    NOT NULL,
    calc_type          TEXT    NOT NULL,
    method             TEXT    NOT NULL,
    basis_set          TEXT    NOT NULL DEFAULT '',
    charge             INTEGER DEFAULT 0,
    multiplicity       INTEGER DEFAULT 1,
    formula            TEXT    NOT NULL DEFAULT '',
    n_atoms            INTEGER DEFAULT 0,
    energy_hartree     REAL,
    energy_ev          REAL,
    energy_kcalmol     REAL,
    opt_converged      INTEGER,
    has_imaginary_freq INTEGER,
    lowest_freq_cm1    REAL,
    enthalpy           REAL,
    gibbs_energy       REAL,
    wall_time_seconds  INTEGER,
    status             TEXT    NOT NULL DEFAULT 'completed',
    indexed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dft_method  ON dft_calculations(method);
CREATE INDEX IF NOT EXISTS idx_dft_formula ON dft_calculations(formula);
CREATE INDEX IF NOT EXISTS idx_dft_status  ON dft_calculations(status);
CREATE INDEX IF NOT EXISTS idx_dft_energy  ON dft_calculations(energy_hartree);
CREATE INDEX IF NOT EXISTS idx_dft_mtime   ON dft_calculations(mtime);
"""


class DFTIndexStore:
    def __init__(self) -> None:
        self._db: sqlite3.Connection | None = None
        self._db_path: str = ""
        self._lock = threading.Lock()

    @property
    def db_path(self) -> str:
        return self._db_path

    def initialize(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA_SQL)
        self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _require_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("DFTIndex has not been initialized yet.")
        return self._db

    def existing_signatures(self) -> dict[str, tuple[str, str]]:
        db = self._require_db()
        with self._lock:
            cursor = db.execute("SELECT source_path, file_hash, status FROM dft_calculations")
            return {row["source_path"]: (row["file_hash"], str(row["status"])) for row in cursor}

    def apply_index_changes(self, *, remove_paths: set[str], results: list[Any]) -> int:
        db = self._require_db()
        removed = 0
        with self._lock:
            for source_path in remove_paths:
                db.execute("DELETE FROM dft_calculations WHERE source_path = ?", (source_path,))
                removed += 1
            for result in results:
                self._upsert_unlocked(db, result)
            db.commit()
        return removed

    def upsert_result(self, result: Any) -> None:
        db = self._require_db()
        with self._lock:
            self._upsert_unlocked(db, result)
            db.commit()

    def _upsert_unlocked(self, db: sqlite3.Connection, result: Any) -> None:
        db.execute(
            """INSERT INTO dft_calculations (
                source_path, file_hash, mtime, calc_type, method, basis_set,
                charge, multiplicity, formula, n_atoms,
                energy_hartree, energy_ev, energy_kcalmol,
                opt_converged, has_imaginary_freq, lowest_freq_cm1,
                enthalpy, gibbs_energy, wall_time_seconds, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                file_hash=excluded.file_hash,
                mtime=excluded.mtime,
                calc_type=excluded.calc_type,
                method=excluded.method,
                basis_set=excluded.basis_set,
                charge=excluded.charge,
                multiplicity=excluded.multiplicity,
                formula=excluded.formula,
                n_atoms=excluded.n_atoms,
                energy_hartree=excluded.energy_hartree,
                energy_ev=excluded.energy_ev,
                energy_kcalmol=excluded.energy_kcalmol,
                opt_converged=excluded.opt_converged,
                has_imaginary_freq=excluded.has_imaginary_freq,
                lowest_freq_cm1=excluded.lowest_freq_cm1,
                enthalpy=excluded.enthalpy,
                gibbs_energy=excluded.gibbs_energy,
                wall_time_seconds=excluded.wall_time_seconds,
                status=excluded.status,
                indexed_at=CURRENT_TIMESTAMP
            """,
            (
                result.source_path,
                result.file_hash,
                result.mtime,
                result.calc_type,
                result.method,
                result.basis_set,
                result.charge,
                result.multiplicity,
                result.formula,
                result.n_atoms,
                result.energy_hartree,
                result.energy_ev,
                result.energy_kcalmol,
                1
                if result.opt_converged is True
                else (0 if result.opt_converged is False else None),
                1
                if result.has_imaginary_freq is True
                else (0 if result.has_imaginary_freq is False else None),
                result.lowest_freq_cm1,
                result.enthalpy,
                result.gibbs_energy,
                result.wall_time_seconds,
                result.status,
            ),
        )

    def count(self) -> int:
        db = self._require_db()
        with self._lock:
            cursor = db.execute("SELECT COUNT(*) FROM dft_calculations")
            row = cursor.fetchone()
        return row[0] if row else 0

    def query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        db = self._require_db()
        with self._lock:
            cursor = db.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        db = self._require_db()
        with self._lock:
            stats: dict[str, Any] = {}

            cursor = db.execute("SELECT COUNT(*) FROM dft_calculations")
            row = cursor.fetchone()
            stats["total"] = row[0] if row else 0

            cursor = db.execute(
                "SELECT status, COUNT(*) as cnt FROM dft_calculations GROUP BY status"
            )
            stats["by_status"] = {row["status"]: row["cnt"] for row in cursor}

            cursor = db.execute(
                "SELECT method, COUNT(*) as cnt FROM dft_calculations "
                "GROUP BY method ORDER BY cnt DESC LIMIT 10"
            )
            stats["by_method"] = {row["method"]: row["cnt"] for row in cursor}

            cursor = db.execute(
                "SELECT calc_type, COUNT(*) as cnt FROM dft_calculations "
                "GROUP BY calc_type ORDER BY cnt DESC"
            )
            stats["by_calc_type"] = {row["calc_type"]: row["cnt"] for row in cursor}

            cursor = db.execute(
                "SELECT formula, COUNT(*) as cnt FROM dft_calculations "
                "GROUP BY formula ORDER BY cnt DESC LIMIT 10"
            )
            stats["top_formulas"] = {row["formula"]: row["cnt"] for row in cursor}

        return stats

from __future__ import annotations

import sqlite3
from pathlib import Path

from evidence_evolve.models import Budgets


class BudgetExceeded(RuntimeError):
    pass


class BudgetLedger:
    """SQLite-backed, idempotent budget reservations."""

    def __init__(self, database: Path, budgets: Budgets):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(budgets)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self, budgets: Budgets) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_limits (
                    category TEXT PRIMARY KEY,
                    hard_limit INTEGER NOT NULL CHECK (hard_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    units INTEGER NOT NULL CHECK (units > 0),
                    FOREIGN KEY(category) REFERENCES budget_limits(category)
                );
                """
            )
            for category, limit in budgets.model_dump().items():
                existing = connection.execute(
                    "SELECT hard_limit FROM budget_limits WHERE category = ?",
                    (category,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO budget_limits(category, hard_limit) VALUES (?, ?)",
                        (category, limit),
                    )
                elif existing[0] != limit:
                    raise ValueError(
                        f"budget limit drift for {category}: db={existing[0]} contract={limit}"
                    )

    def reserve(self, category: str, units: int, reservation_key: str) -> bool:
        if units <= 0:
            raise ValueError("units must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT category, units FROM budget_reservations "
                "WHERE reservation_key = ?",
                (reservation_key,),
            ).fetchone()
            if prior is not None:
                if prior != (category, units):
                    raise ValueError(
                        "reservation key already exists with different category or units"
                    )
                connection.commit()
                return False
            row = connection.execute(
                "SELECT hard_limit FROM budget_limits WHERE category = ?",
                (category,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown budget category: {category}")
            used = connection.execute(
                "SELECT COALESCE(SUM(units), 0) FROM budget_reservations "
                "WHERE category = ?",
                (category,),
            ).fetchone()[0]
            if used + units > row[0]:
                raise BudgetExceeded(
                    f"{category} budget exceeded: used={used}, requested={units}, limit={row[0]}"
                )
            connection.execute(
                "INSERT INTO budget_reservations(reservation_key, category, units) "
                "VALUES (?, ?, ?)",
                (reservation_key, category, units),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT l.category, l.hard_limit, COALESCE(SUM(r.units), 0)
                FROM budget_limits AS l
                LEFT JOIN budget_reservations AS r ON r.category = l.category
                GROUP BY l.category, l.hard_limit
                ORDER BY l.category
                """
            ).fetchall()
        return {
            category: {"limit": limit, "used": used, "remaining": limit - used}
            for category, limit, used in rows
        }


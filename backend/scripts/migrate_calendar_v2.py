"""Reversible Calendar V2 schema migration for installations without Alembic.

Normal application startup applies the idempotent upgrade statements through
``core.database.ADMIN_DDL``.  This script exposes explicit upgrade/downgrade
operations for audited roll-forward/rollback procedures.
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from core.database import admin_engine


UPGRADE_STATEMENTS = (
    "ALTER TABLE academic_calendar ADD COLUMN IF NOT EXISTS academic_year VARCHAR(9)",
    "ALTER TABLE academic_calendar ADD COLUMN IF NOT EXISTS term VARCHAR(16)",
    "ALTER TABLE academic_calendar ADD COLUMN IF NOT EXISTS aliases TEXT NOT NULL DEFAULT '[]'",
)

DOWNGRADE_STATEMENTS = (
    "ALTER TABLE academic_calendar DROP COLUMN IF EXISTS aliases",
    "ALTER TABLE academic_calendar DROP COLUMN IF EXISTS term",
    "ALTER TABLE academic_calendar DROP COLUMN IF EXISTS academic_year",
)


def apply_calendar_v2_migration(direction: str, *, engine=admin_engine) -> None:
    statements = UPGRADE_STATEMENTS if direction == "upgrade" else DOWNGRADE_STATEMENTS
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def main() -> None:
    parser = argparse.ArgumentParser(description="Academic Calendar V2 migration")
    parser.add_argument("direction", choices=("upgrade", "downgrade"))
    args = parser.parse_args()
    apply_calendar_v2_migration(args.direction)


if __name__ == "__main__":
    main()

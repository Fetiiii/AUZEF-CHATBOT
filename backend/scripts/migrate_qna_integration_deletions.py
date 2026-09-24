"""Reversible tombstone-table migration for QnA integration sync.

Normal schema initialization creates the model through ``create_all``. This
script provides explicit upgrade/downgrade operations for audited deployments.
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from core.database import admin_engine


UPGRADE_STATEMENTS = (
    "ALTER TABLE qna ALTER COLUMN created_at "
    "SET DEFAULT timezone('UTC', clock_timestamp())",
    "ALTER TABLE qna ALTER COLUMN updated_at "
    "SET DEFAULT timezone('UTC', clock_timestamp())",
    """
    CREATE TABLE IF NOT EXISTS qna_integration_deletions (
        qna_id BIGINT PRIMARY KEY,
        deleted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
            DEFAULT timezone('UTC', clock_timestamp())
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_qna_integration_deletions_deleted_at "
    "ON qna_integration_deletions (deleted_at)",
    "ALTER TABLE qna_integration_deletions ALTER COLUMN deleted_at "
    "SET DEFAULT timezone('UTC', clock_timestamp())",
)

DOWNGRADE_STATEMENTS = ("DROP TABLE IF EXISTS qna_integration_deletions",)


def apply_qna_integration_deletions_migration(
    direction: str, *, engine=admin_engine
) -> None:
    statements = UPGRADE_STATEMENTS if direction == "upgrade" else DOWNGRADE_STATEMENTS
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def main() -> None:
    parser = argparse.ArgumentParser(description="QnA integration tombstone migration")
    parser.add_argument("direction", choices=("upgrade", "downgrade"))
    args = parser.parse_args()
    apply_qna_integration_deletions_migration(args.direction)


if __name__ == "__main__":
    main()

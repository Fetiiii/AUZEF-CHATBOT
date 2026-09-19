"""Reversible Phase 6 AI model registry migration (installations without Alembic).

Normal initialization (``init_system db``) creates the tables through
``Base.metadata.create_all`` + the idempotent ``ADMIN_DDL`` append-only
triggers, then runs the idempotent bootstrap.  This script exposes explicit
upgrade/downgrade operations for audited roll-forward/rollback procedures.

Downgrade drops the registry, capability config, version history and audit
tables. After a downgrade the runtime falls back to the pre-Phase-6 env/default
path (``NOT_CONFIGURED`` → ``ENV_BOOTSTRAP``), i.e. Phase 5 behavior.
No table holds API secrets.
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from core.database import (
    AI_APPEND_ONLY_DDL,
    AICapabilityConfig,
    AIConfigAudit,
    AIConfigVersion,
    AIModelRegistry,
    Base,
    admin_engine,
)

AI_TABLES = (
    AIModelRegistry.__table__,
    AIConfigVersion.__table__,
    AICapabilityConfig.__table__,
    AIConfigAudit.__table__,
)

DOWNGRADE_STATEMENTS = (
    "DROP TABLE IF EXISTS ai_capability_config",
    "DROP TABLE IF EXISTS ai_config_audit",
    "DROP TABLE IF EXISTS ai_config_version",
    "DROP TABLE IF EXISTS ai_model_registry",
    "DROP FUNCTION IF EXISTS ai_append_only_guard()",
)


def apply_model_registry_migration(direction: str, *, engine=admin_engine) -> None:
    if direction == "upgrade":
        Base.metadata.create_all(bind=engine, tables=list(AI_TABLES))
        with engine.begin() as connection:
            for statement in AI_APPEND_ONLY_DDL:
                connection.execute(text(statement))
        return
    with engine.begin() as connection:
        for statement in DOWNGRADE_STATEMENTS:
            connection.execute(text(statement))


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 AI model registry migration")
    parser.add_argument("direction", choices=("upgrade", "downgrade"))
    args = parser.parse_args()
    apply_model_registry_migration(args.direction)


if __name__ == "__main__":
    main()

"""Read-only pilot DB/config validation (Gate A-04 evidence).

Run in the pilot backend container:
  docker exec -w /app auzef_backend python -m scripts.pilot_db_validation [--json] [--academic-year 2026-2027]

Checks (never writes; the session is rolled back):
  * academic calendar rows for the pilot academic year, per term; legacy rows without a year
  * ACADEMIC_CALENDAR_CURRENT_YEAR / _CURRENT_TERM (DB value, else env) — set and consistent with the rows
  * LLM_ENABLED live value, maintenance flag
  * admin roles: count of active super_admin / admin / editor (no e-mail addresses are printed)
  * managed AI config version and both capability model identities
Exit 0 when every FAIL-level check passes; WARN lines are for the operator.
"""
from __future__ import annotations

import argparse
import json
from datetime import date


def collect(academic_year: str) -> list[dict]:
    from sqlalchemy import func

    from core.database import AcademicCalendar, AdminUser, SessionLocal, SystemConfig
    from services import ai_registry
    from services.calendar_retrieval import resolve_calendar_runtime_config
    from services.llm_config import LLMCapability

    checks = []

    def add(name, level, ok, expected, actual):
        checks.append({"name": name, "level": level, "status": "PASS" if ok else level,
                       "expected": expected, "actual": actual})

    db = SessionLocal()
    try:
        by_term = dict(db.query(AcademicCalendar.term, func.count()).filter(
            AcademicCalendar.academic_year == academic_year).group_by(AcademicCalendar.term).all())
        legacy = db.query(func.count()).select_from(AcademicCalendar).filter(
            AcademicCalendar.academic_year.is_(None)).scalar()
        add("calendar_rows_for_pilot_year", "FAIL", sum(by_term.values()) > 0,
            f"rows for {academic_year}", {str(k): v for k, v in by_term.items()})
        add("calendar_has_both_terms", "WARN", {"GUZ", "BAHAR"} <= {str(k) for k in by_term},
            "GUZ and BAHAR rows", sorted(str(k) for k in by_term))
        add("calendar_legacy_rows_without_year", "WARN", legacy == 0, 0, legacy)
        cfg = resolve_calendar_runtime_config(db)
        add("calendar_current_year_set", "FAIL", cfg.current_academic_year == academic_year,
            academic_year, f"{cfg.current_academic_year} (source {cfg.year_source})")
        term = cfg.current_term.value if cfg.current_term else None
        add("calendar_current_term_set", "FAIL", term in ("GUZ", "BAHAR"), "GUZ or BAHAR",
            f"{term} (source {cfg.term_source})")
        today = date.today()
        expected_term = "GUZ" if today.month >= 9 or today.month == 1 else "BAHAR"
        add("calendar_current_term_matches_today", "WARN", term == expected_term,
            f"{expected_term} for {today.isoformat()} (approximation; confirm with the academic calendar)", term)
        values = {r.key: r.value for r in db.query(SystemConfig).filter(
            SystemConfig.key.in_(("LLM_ENABLED", "MAINTENANCE_MODE"))).all()}
        add("llm_enabled_live_true", "FAIL", (values.get("LLM_ENABLED") or "").lower() == "true", "true",
            values.get("LLM_ENABLED"))
        add("maintenance_off", "WARN", (values.get("MAINTENANCE_MODE") or "false").lower() != "true", "false",
            values.get("MAINTENANCE_MODE"))
        roles = dict(db.query(AdminUser.role, func.count()).filter(AdminUser.is_active == 1)
                     .group_by(AdminUser.role).all())
        add("active_super_admin_exists", "FAIL", roles.get("super_admin", 0) >= 1, ">= 1",
            {str(k): v for k, v in roles.items()})
        active = ai_registry.load_active_config(db)
        if active is None:
            add("managed_ai_config", "FAIL", False, "active version", "none")
        else:
            ident = {cap.value: active.assignments[cap].model.model_identifier for cap in LLMCapability}
            add("managed_ai_config", "FAIL", True, "active version", {"version": active.version_id, **ident})
    finally:
        db.rollback()
        db.close()
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--academic-year", default="2026-2027")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = collect(args.academic_year)
    failed = [c for c in checks if c["status"] == "FAIL"]
    status = "FAIL" if failed else "PASS"
    if args.json:
        print(json.dumps({"status": status, "checks": checks}, ensure_ascii=False, indent=1, default=str))
    else:
        for c in checks:
            print(f"  [{c['status']}] {c['name']}: expected {c['expected']} / actual {c['actual']}")
        print(f"\nPILOT_DB_VALIDATION = {status}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

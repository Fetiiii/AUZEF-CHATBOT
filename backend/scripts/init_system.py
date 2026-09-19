"""Operatör tarafından açıkça çalıştırılan infrastructure initialization CLI.

Production APP runtime ``uvicorn main:app`` ile doğrudan başlar ve bu modülü
çağırmaz. Development container entrypoint'i rahatlık için ``all`` komutunu
Uvicorn'dan önce çalıştırır.
"""
import argparse
from collections.abc import Sequence

from core.database import (
    SessionLocal,
    SystemConfig,
    init_admin_db,
    init_chat_db,
)
from services.ai_registry import bootstrap_ai_registry


DEFAULT_SYSTEM_CONFIG = {
    "LLM_ENABLED": "false",
}


def seed_default_config() -> None:
    """Eksik başlangıç ayarlarını ekle; mevcut operatör değerlerini koru.

    AI model registry'si de burada idempotent olarak bootstrap edilir: Phase 0
    varsayılan modelleri (LEGACY_APPROVED) ve — henüz versiyon yoksa — env'in
    bugünkü effective config'iyle birebir aynı ilk config versiyonu."""
    db = SessionLocal()
    try:
        for key, default_value in DEFAULT_SYSTEM_CONFIG.items():
            row = db.get(SystemConfig, key)
            if row is None:
                db.add(SystemConfig(key=key, value=default_value))
                print(f"⚙️ '{key}' ayarı '{default_value}' olarak set edildi.")
            else:
                print(f"ℹ️ '{key}' zaten var: {row.value}")
        result = bootstrap_ai_registry(db)
        print(f"🤖 AI model registry bootstrap: {result}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_db() -> None:
    """DB-ADMIN, DB-CHAT ve başlangıç config değerlerini hazırla."""
    print("🚀 Veritabanı kurulumu başlıyor...")
    init_admin_db()
    init_chat_db()
    seed_default_config()
    print("✅ Veritabanı kurulumu tamamlandı.")


def initialize_qdrant() -> None:
    """Qdrant collection'ını açık ve idempotent provisioning adımıyla hazırla."""
    # Ağır provider/model yalnız qdrant veya all komutunda yüklensin; salt DB
    # initialization bu runtime bağımlılığını gerektirmesin.
    from core.deps import QDRANT_PROVIDER

    print("🚀 Qdrant collection kurulumu başlıyor...")
    QDRANT_PROVIDER.ensure_collection()
    print("✅ Qdrant collection kurulumu tamamlandı.")


def initialize_all() -> None:
    """Önce DB, ardından Qdrant infrastructure initialization çalıştır."""
    initialize_db()
    initialize_qdrant()


def setup() -> None:
    """Eski Python çağrıları için ``all`` davranışlı uyumluluk yardımcısı."""
    initialize_all()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AUZEF Chatbot infrastructure initialization",
    )
    parser.add_argument(
        "command",
        choices=("db", "qdrant", "all"),
        help="db schema/config, qdrant collection veya sıralı olarak tümü",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    command = _parser().parse_args(argv).command
    if command == "db":
        initialize_db()
    elif command == "qdrant":
        initialize_qdrant()
    else:
        initialize_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

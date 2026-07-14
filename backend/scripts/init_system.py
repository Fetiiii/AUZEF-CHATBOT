from core.database import init_db, SessionLocal, SystemConfig
from sqlalchemy import text

def setup():
    # 1. DB ve View'ları Kur
    print("🚀 Veritabanı kurulumu başlıyor...")
    init_db()

    # 2. Başlangıç Konfigürasyonlarını Ekle
    db = SessionLocal()
    try:
        # LLM_ENABLED ayarı
        llm_config = db.query(SystemConfig).filter(SystemConfig.key == "LLM_ENABLED").first()
        if not llm_config:
            new_config = SystemConfig(key="LLM_ENABLED", value="false")
            db.add(new_config)
            db.commit()
            print("⚙️ 'LLM_ENABLED' ayarı 'false' olarak set edildi.")
        else:
            print(f"ℹ️ 'LLM_ENABLED' zaten var: {llm_config.value}")

    except Exception as e:
        print(f"❌ Hata: {str(e)}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    setup()

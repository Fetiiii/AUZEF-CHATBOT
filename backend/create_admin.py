"""Admin kullanıcısı oluşturma/güncelleme CLI'ı.

Kullanım (container içinde ya da yerelde):
    python create_admin.py ornek@istanbul.edu.tr                # parolayı gizli sorar
    python create_admin.py ornek@istanbul.edu.tr --name "Ad Soyad"
    python create_admin.py ornek@istanbul.edu.tr --deactivate   # erişimi kapat

Var olan e-posta için çalıştırılırsa parolayı/adı GÜNCELLER (parola sıfırlama
bu şekilde yapılır). Deaktive edilen kullanıcının açık oturumları da silinir.
"""
import argparse
import getpass
import sys

from database import SessionLocal, AdminUser, AdminSession, init_db
from auth import hash_password


def main():
    parser = argparse.ArgumentParser(description="Admin kullanıcısı oluştur/güncelle")
    parser.add_argument("email", help="Kullanıcı e-postası")
    parser.add_argument("--name", default=None, help="Ad Soyad (opsiyonel)")
    parser.add_argument("--deactivate", action="store_true", help="Kullanıcının erişimini kapat")
    args = parser.parse_args()

    email = args.email.strip().lower()
    init_db()  # tablolar yoksa oluştur (idempotent)

    db = SessionLocal()
    try:
        user = db.query(AdminUser).filter(AdminUser.email == email).first()

        if args.deactivate:
            if user is None:
                print(f"❌ Kullanıcı bulunamadı: {email}")
                sys.exit(1)
            user.is_active = 0
            db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
            db.commit()
            print(f"✅ Erişim kapatıldı ve açık oturumları silindi: {email}")
            return

        password = getpass.getpass("Parola: ")
        if len(password) < 10:
            print("❌ Parola en az 10 karakter olmalı.")
            sys.exit(1)
        if password != getpass.getpass("Parola (tekrar): "):
            print("❌ Parolalar eşleşmiyor.")
            sys.exit(1)

        if user is None:
            user = AdminUser(email=email, password_hash=hash_password(password), full_name=args.name, is_active=1)
            db.add(user)
            db.commit()
            print(f"✅ Admin kullanıcısı oluşturuldu: {email}")
        else:
            user.password_hash = hash_password(password)
            user.is_active = 1
            if args.name:
                user.full_name = args.name
            # Parola değişti: eski oturumlar güvenlik gereği kapatılır.
            db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
            db.commit()
            print(f"✅ Kullanıcı güncellendi (parola sıfırlandı, oturumlar kapatıldı): {email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

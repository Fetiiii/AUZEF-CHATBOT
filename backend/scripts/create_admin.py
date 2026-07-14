"""Admin kullanıcısı oluşturma/güncelleme CLI'ı.

Bu CLI esas olarak İLK super_admin'i oluşturmak (bootstrap) ve panele
girilemeyen acil durumlar içindir — gündelik kullanıcı yönetimi artık
ayarlar sayfasından yapılır (yalnızca super_admin erişir).

Kullanım (container içinde ya da yerelde):
    python create_admin.py ornek@istanbul.edu.tr                     # yeni kullanıcı (super_admin)
    python create_admin.py ornek@istanbul.edu.tr --role editor       # rol belirterek
    python create_admin.py ornek@istanbul.edu.tr --name "Ad Soyad"
    python create_admin.py ornek@istanbul.edu.tr --deactivate        # erişimi kapat

Var olan e-posta için çalıştırılırsa GÜNCELLEME yapar:
- Parola sorulduğunda BOŞ bırakılırsa parola değişmez (yalnızca rol/ad güncellenir).
- Parola girilirse sıfırlanır ve açık oturumlar güvenlik gereği kapatılır.
- --role yalnızca açıkça verildiyse mevcut kullanıcının rolünü değiştirir.
"""
import argparse
import getpass
import sys

from core.database import SessionLocal, AdminUser, AdminSession, init_db
from admin.auth import hash_password, VALID_ROLES


def _ask_password(optional: bool) -> str:
    prompt = "Parola (boş bırak = değiştirme): " if optional else "Parola: "
    password = getpass.getpass(prompt)
    if optional and not password:
        return ""
    if len(password) < 10:
        print("❌ Parola en az 10 karakter olmalı.")
        sys.exit(1)
    if password != getpass.getpass("Parola (tekrar): "):
        print("❌ Parolalar eşleşmiyor.")
        sys.exit(1)
    return password


def main():
    parser = argparse.ArgumentParser(description="Admin kullanıcısı oluştur/güncelle")
    parser.add_argument("email", help="Kullanıcı e-postası")
    parser.add_argument("--name", default=None, help="Ad Soyad (opsiyonel)")
    parser.add_argument("--role", default=None, choices=sorted(VALID_ROLES),
                        help="Rol (yeni kullanıcıda varsayılan: super_admin; "
                             "mevcut kullanıcıda yalnızca verilirse değiştirir)")
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

        if user is None:
            # CLI'dan oluşturulan kullanıcı tipik olarak sistemin ilk yöneticisidir.
            role = args.role or "super_admin"
            password = _ask_password(optional=False)
            user = AdminUser(email=email, password_hash=hash_password(password),
                             full_name=args.name, role=role, is_active=1)
            db.add(user)
            db.commit()
            print(f"✅ Kullanıcı oluşturuldu: {email} (rol: {role})")
            return

        # Mevcut kullanıcı: güncelleme
        changed = []
        password = _ask_password(optional=True)
        if password:
            user.password_hash = hash_password(password)
            db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
            changed.append("parola sıfırlandı, oturumlar kapatıldı")
        if args.role and args.role != user.role:
            user.role = args.role
            db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
            changed.append(f"rol → {args.role}")
        if args.name:
            user.full_name = args.name
            changed.append("ad güncellendi")
        user.is_active = 1  # güncelleme = erişim açık kabul edilir

        db.commit()
        print(f"✅ Kullanıcı güncellendi: {email} ({'; '.join(changed) or 'değişiklik yok'})")
    finally:
        db.close()


if __name__ == "__main__":
    main()

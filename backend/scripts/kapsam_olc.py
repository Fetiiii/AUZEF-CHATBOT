"""Test kayitlarini GECICI olarak gizleyip botun o sorulara ne dedigini olcer.

NEDEN: 56 test kaydi indekste dururken "bu soruyu mevcut KB zaten cevapliyor
mu?" sorusu disaridan olculemiyor — alias'lari guclu bir test kaydi, mevcut
cevap da iyi olsa bile yarisi kazaniyor ve gorunmez oluyor. Tek dogru olcum
kayitlari gecici olarak indeksten cikarip sormak.

AKIS (try/finally ile korumali):
  1. Test kayitlarini pasife al (status=0) ve indekslerden cikar
  2. Her kaydin kanonik sorusunu bota sor, cevabi yaz
  3. NE OLURSA OLSUN geri al (status=1) ve yeniden indeksle

Adim 3 finally icinde: script yarida kesilse de kayitlar geri gelir. Yine de
bittikten sonra ciktidaki "geri alindi" satirini gormeden birakmayin.

Kullanim (sunucuda, test eden kimse yokken):
    docker exec -e PYTHONPATH=/app -w /app auzef_backend \
        python scripts/kapsam_olc.py > /tmp/kapsam.txt 2>&1
"""

import json
import re
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.database import QnA, SessionLocal, execute_admin_sql, execute_chat_sql
from routers.qna import remove_from_providers, sync_providers

CHAT_URL = "http://localhost:8000/widget-chat"
ISARET = "analiz doğrulama testi"


def _indeksten_gittigini_bekle(ids: list[int], timeout: float = 60.0) -> None:
    """Kayitlar Qdrant VE Meili'den gercekten silinene kadar bekler."""
    from core.deps import MEILI_PROVIDER, QDRANT_PROVIDER

    hedef = set(ids)
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        kalan = set()
        try:
            noktalar = QDRANT_PROVIDER.client.retrieve(
                collection_name=QDRANT_PROVIDER.collection_name, ids=list(hedef)
            )
            kalan |= {point.id for point in noktalar}
        except Exception:  # noqa: BLE001 — erisilemezse Meili kontrolu yeter
            pass
        for qna_id in hedef:
            try:
                # Silinmis dokuman icin Meili istisna atar; ISTEDIGIMIZ bu.
                # try/except her id icin AYRI: tek bir istisna dongudeki
                # digerlerini atlatirsa kalanlari hic kontrol etmemis oluruz.
                if MEILI_PROVIDER.index.get_document(qna_id) is not None:
                    kalan.add(qna_id)
            except Exception:  # noqa: BLE001 — "document not found" beklenen
                pass
        if not kalan:
            return
        time.sleep(1.0)
    print(f"     UYARI: {len(kalan)} kayit {timeout:.0f} sn icinde indeksten silinmedi.")


def sor(db, soru: str) -> tuple[str, bool]:
    """Soruyu sorar; (cevap, cevaplayabildi_mi) doner.

    CEVAPLAYAMAMA METINDEN DEGIL, `source` ALANINDAN OKUNUR. Metne bakmak
    bir kez yanildi: bot iki ayri sekilde reddediyor —
      * "Bu konuda bilgim bulunmuyor."
      * "Bu konuda net bir bilgim yok. Sunlari sormak istemis olabilirsiniz:"
    Yalniz birincisini arayan olcum, ikinci kalibi "bot cevap verdi" sayip
    7 gercek boslugu kapsanmis gosterdi. `routers/chat.py` her iki dalda da
    source="none" yaziyor; metin degisse bile bu alan dogru kalir.
    """
    body = json.dumps({"message": soru}).encode()
    request = urllib.request.Request(CHAT_URL, data=body, headers={"Content-Type": "application/json"})
    payload = json.loads(urllib.request.urlopen(request, timeout=120).read())
    cevap = payload["answer"]

    message_id = payload.get("message_id")
    if message_id is None:
        # Mesaj kaydedilememis (best-effort); son care olarak metne bak.
        return cevap, "bilgim bulunmuyor" not in cevap and "net bir bilgim yok" not in cevap
    source = execute_chat_sql(
        db,
        text("SELECT source FROM conversation_messages WHERE id = :id"), {"id": message_id}
    ).scalar()
    return cevap, source not in (None, "none")


def main() -> None:
    db = SessionLocal()
    rows = execute_admin_sql(
        db,
        text(
            "SELECT id, question_text, answer_text FROM qna "
            "WHERE answer_text LIKE :p ORDER BY id"
        ),
        {"p": f"%{ISARET}%"},
    ).mappings().all()

    if not rows:
        print("Test kaydi bulunamadi — olcecek bir sey yok.")
        return

    kayitlar = [
        (r["id"], re.search(r"\[(TEST-K\d+)\]", r["answer_text"]).group(1), r["question_text"])
        for r in rows
    ]
    print(f"{len(kayitlar)} test kaydi bulundu.\n")

    try:
        print("1/3  kayitlar gizleniyor...")
        for qna_id, _, _ in kayitlar:
            db.query(QnA).filter(QnA.id == qna_id).update({"status": 0})
        db.commit()
        for qna_id, _, _ in kayitlar:
            remove_from_providers(qna_id)
        # MEILI SILMESI ASENKRON: istek bir gorev kuyruga atar, indeks hemen
        # guncellenmez. Beklemeden olcersek gizledigimiz kayitlar donmeye
        # devam eder — provada tam olarak bu oldu. Gercekten gittigini
        # dogruladiktan sonra olcuyoruz.
        _indeksten_gittigini_bekle([qna_id for qna_id, _, _ in kayitlar])
        print("     indekslerden cikarildi.\n")

        print("2/3  sorular soruluyor...")
        sonuclar = []
        for index, (_, mark, soru) in enumerate(kayitlar, start=1):
            try:
                cevap, cevaplandi = sor(db, soru)
            except Exception as exc:  # noqa: BLE001 — olcum, her hata raporlanir
                cevap, cevaplandi = f"HATA: {type(exc).__name__}", False
            sonuclar.append((mark, soru, cevap, cevaplandi))
            if index % 10 == 0:
                print(f"     {index}/{len(kayitlar)}")
            time.sleep(0.5)
        print()
    finally:
        print("3/3  kayitlar GERI ALINIYOR...")
        for qna_id, _, _ in kayitlar:
            db.query(QnA).filter(QnA.id == qna_id).update({"status": 1})
        db.commit()
        for qna_id, _, _ in kayitlar:
            sync_providers(db, qna_id)
        geri = execute_admin_sql(
            db,
            text("SELECT count(*) FROM qna WHERE answer_text LIKE :p AND status = 1"),
            {"p": f"%{ISARET}%"},
        ).scalar()
        print(f"     geri alindi: {geri}/{len(kayitlar)} kayit aktif ve yeniden indekslendi.\n")

    bilmiyorum = [s for s in sonuclar if not s[3]]
    cevapli = [s for s in sonuclar if s[3]]
    print("=" * 72)
    print(f"BOT CEVAP VEREMEDI (kesin bosluk):  {len(bilmiyorum)}/{len(sonuclar)}")
    print(f"BOT BIR CEVAP VERDI (INCELENMELI):  {len(cevapli)}/{len(sonuclar)}")
    print("=" * 72)
    print(
        "\nDIKKAT: 'bir cevap verdi' KAPSANIYOR DEMEK DEGILDIR. Bot neredeyse\n"
        "hicbir soruyu bos birakmiyor; donen metin cogu zaman BASKA bir konuyu\n"
        "anlatiyor (olculdu: 41 cevabin 33'u soruyu karsilamiyordu). Asagidaki\n"
        "bolumu tek tek okumadan karar vermeyin."
    )

    print("\n--- BOT CEVAP VEREMEDI: bu kayitlara cevap YAZILMALI ---")
    for mark, soru, _, _ in bilmiyorum:
        print(f"  {mark}  {soru}")

    print("\n--- BOT CEVAP VERDI: cevabin soruyu karsilayip karsilamadigini okuyun ---")
    for mark, soru, cevap, _ in cevapli:
        print(f"\n{'=' * 72}\n{mark}  {soru}\n{'-' * 72}\n{cevap[:600]}")


if __name__ == "__main__":
    main()

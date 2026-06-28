import os
import csv
from database import SessionLocal, AcademicCalendar

def import_calendar():
    db = SessionLocal()
    file_path = "data/auzef_akademik_takvim_2025_2026_guncel.csv"
    
    if not os.path.exists(file_path):
        print(f"❌ Dosya bulunamadı: {file_path}")
        db.close()
        return

    # Check delimiter
    with open(file_path, 'r', encoding='utf-8') as f:
        first_line = f.readline()
        delimiter = ';' if ';' in first_line else ','

    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        inserted = 0
        
        # Clear existing entries first to avoid duplicates if run multiple times
        db.query(AcademicCalendar).delete()
        
        for row in reader:
            period = (row.get("Donem") or row.get("period") or "").strip()
            event = (row.get("Etkinlik") or row.get("event") or "").strip()
            start = (row.get("Baslangic_Tarihi") or row.get("start_date") or "").strip()
            end = (row.get("Bitis_Tarihi") or row.get("end_date") or "").strip()

            if not event or not start:
                continue

            db.add(AcademicCalendar(
                period=period,
                event=event,
                start_date=start,
                end_date=end or start,
            ))
            inserted += 1

        db.commit()
        print(f"✅ Akademik takvim verileri başarıyla yüklendi. Eklenen satır sayısı: {inserted}")
    db.close()

if __name__ == "__main__":
    import_calendar()

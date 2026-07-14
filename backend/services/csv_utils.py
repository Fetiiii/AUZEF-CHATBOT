"""CSV yardımcıları: formül enjeksiyonu koruması + streaming CSV yanıtı."""
import csv
import io

from fastapi.responses import StreamingResponse


def csv_safe(value) -> str:
    """CSV formül enjeksiyonuna karşı hücre koruması.

    Kullanıcı metni CSV'ye ham yazılırsa "=HYPERLINK(...)" gibi bir mesaj,
    dosyayı Excel'de açan yöneticinin makinesinde formül olarak ÇALIŞIR.
    Tehlikeli önekleri (' ile) etkisizleştiriyoruz."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t"):
        return "'" + s
    return s


def stream_csv(header: list, rows_iter, filename: str, delimiter: str = ";") -> StreamingResponse:
    """CSV'yi parça parça (streaming) döner — tüm dosyayı bellekte kurmaz.

    rows_iter satır (liste) üreten bir generator'dır; DB'yi öbek öbek çekmesi
    beklenir. Yaklaşık 32 KB biriktikçe bir parça yollanır. İlk parçada BOM
    (\\ufeff) verilir ki Excel Türkçe karakterleri doğru göstersin."""
    def _drain(buf: io.StringIO) -> str:
        data = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return data

    def gen():
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter)
        writer.writerow(header)
        yield "﻿" + _drain(buf)   # BOM + başlık (Excel Türkçe)
        for row in rows_iter:
            writer.writerow(row)
            if buf.tell() > 32768:
                yield _drain(buf)
        tail = _drain(buf)
        if tail:
            yield tail

    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

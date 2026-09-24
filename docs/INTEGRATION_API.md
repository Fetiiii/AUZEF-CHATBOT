# Çözüm Merkezi QnA Entegrasyon API'si

Bu salt-okunur API, AUZEF Chatbot'un PostgreSQL'deki güncel QnA verisini
Çözüm Merkezi uygulamasına sunar. Chatbot'un `/api/search` akışını,
MeiliSearch/Qdrant indekslerini veya yönetim CRUD yetkilerini değiştirmez.
Entegrasyon için bir search endpoint'i henüz sunulmamaktadır.

## Bağlantı ve kimlik doğrulama

Örnek base URL:

```text
https://chatbot.example.edu.tr/api/integrations/v1
```

Her istek yalnız aşağıdaki header ile yapılır:

```http
X-API-Key: <secret>
```

Anahtar query parameter, cookie, JWT veya admin oturumu olarak kabul edilmez.
Eksik/yanlış anahtar `401`, sunucuda `INTEGRATION_API_KEY` tanımlı olmaması
`503` döndürür. API yalnız HTTPS üzerinden kullanılmalıdır. Swagger'daki
`Authorize` alanı `X-API-Key` header'ını destekler; dokümanda secret default'u
veya örneği bulunmaz.

Production'da yüksek entropili anahtar secret yönetim sistemiyle üretilip
`/etc/auzef/backend.env` içinde verilir:

```dotenv
INTEGRATION_API_KEY=
```

Anahtarın kendisi loglanmaz. Nginx production şablonu bu namespace'i istemci
IP'si başına 10 istek/saniye, 20 burst ile sınırlar; aşım `429` döndürür.

## Endpoint'ler

API yalnız şu GET endpoint'lerini sunar:

- `GET /qna`
- `GET /qna/changes`
- `GET /meta`

`POST`, `PUT`, `PATCH`, `DELETE` ve `/search` entegrasyon endpoint'leri yoktur.

### Aktif dataset: `GET /qna`

Parametreler:

- `limit`: varsayılan `500`, minimum `1`, maksimum `1000`
- `cursor`: bir önceki yanıttaki opaque `next_cursor`

```sh
curl --fail --get \
  -H "X-API-Key: $INTEGRATION_API_KEY" \
  --data-urlencode "limit=500" \
  https://chatbot.example.edu.tr/api/integrations/v1/qna
```

```json
{
  "until": "2026-09-23T12:40:00Z",
  "items": [
    {
      "id": 42,
      "question": "Kayıt yenileme nasıl yapılır?",
      "answer": "...",
      "categories": ["Kayıt"],
      "updated_at": "2026-09-22T08:15:00Z"
    }
  ],
  "pagination": {
    "next_cursor": "<opaque-cursor>",
    "has_more": true
  }
}
```

`id`, mevcut `qna.id` BIGINT primary key'idir. Sistemde ayrı bir public UUID
bulunmadığı ve bu primary key kalıcı olduğu için v1 dış kimliği olarak
kullanılır. `categories`, mevcut QnA tag'lerinin alfabetik listesidir. Arama
alias'ları, admin alanları ve Meili/Qdrant alanları dışa açılmaz.

Sayfalama `id` tabanlı keyset pagination'dır. Cursor istemci tarafında
yorumlanmamalı/değiştirilmemelidir. Tüm sayfalarda aynı `until` döner.
Sayfalar tek bir PostgreSQL snapshot'ı değildir: arada değişen kayıtlar sonraki
`/qna/changes?since=<until>` çağrısıyla tamamlanır. `until` belirlenirken
QnA ve tombstone yazmalarıyla kısa süreli DB kilidi alınır; kilit cursor
sayfaları arasında tutulmaz.

### Artımsal senkronizasyon: `GET /qna/changes`

`since` zorunlu, timezone içeren ISO-8601 timestamp'idir. `limit` ve `cursor`
kuralları `/qna` ile aynıdır.

```sh
curl --fail --get \
  -H "X-API-Key: $INTEGRATION_API_KEY" \
  --data-urlencode "since=2026-09-20T10:00:00Z" \
  --data-urlencode "limit=500" \
  https://chatbot.example.edu.tr/api/integrations/v1/qna/changes
```

```json
{
  "since": "2026-09-20T10:00:00Z",
  "until": "2026-09-23T12:40:00Z",
  "items": [
    {
      "id": 42,
      "operation": "upsert",
      "question": "Kayıt yenileme nasıl yapılır?",
      "answer": "...",
      "categories": ["Kayıt"],
      "updated_at": "2026-09-22T08:15:00Z"
    },
    {
      "id": 7,
      "operation": "delete",
      "updated_at": "2026-09-23T11:05:00Z"
    }
  ],
  "pagination": {
    "next_cursor": null,
    "has_more": false
  }
}
```

Sıralama `updated_at`, `id`, `operation` üçlüsüyle deterministiktir; aynı
timestamp'e sahip kayıtlar kaybolmaz. `since` sınırı dahildir; sınırdaki
değişiklik tekrar gelebilir, bu nedenle `upsert`/`delete` ID bazında idempotent
uygulanmalıdır. Cursor, ilk sayfadaki `since` ve
sunucu üretimli `until` sınırlarını taşır. Sonraki sayfalarda aynı `since`
ve response cursor'ı kullanılmalıdır.

Aktif kayıt `upsert`, pasife alınmış kayıt `delete` olarak döner. Mevcut
yönetim akışı QnA'yı fiziksel sildiği için delete işlemi aynı DB
transaction'ında `qna_integration_deletions` tombstone tablosuna yazılır.
Böylece kayıt `qna` tablosundan kalksa da silme bilgisi kaybolmaz.

### Dataset durumu: `GET /meta`

```json
{
  "dataset_version": "2026-09-23T12:40:00Z|upsert|42",
  "total_active_records": 3247,
  "last_updated_at": "2026-09-23T12:40:00Z"
}
```

`dataset_version`, son değişikliğin `timestamp|operation|id` bileşimidir;
timestamp eşitliğinde de deterministiktir. Boş ve hiç değişmemiş datasette
version/timestamp `null`, count `0` olur.

## Önerilen senkronizasyon

### İlk kurulum

1. `/qna` sayfalarını `has_more=false` olana kadar takip edin.
2. Sayfaların ortak `until` değerini saklayın.
3. Full sync başarıyla kendi veritabanınıza uygulandıktan sonra
   `last_successful_sync = until` yapın.

### Sonraki senkronizasyonlar

1. `/qna/changes?since=<last_successful_sync>` çağırın.
2. Bütün cursor sayfalarını aynı `since` ile tamamlayın.
3. `upsert` kayıtlarını ekleyin/güncelleyin, `delete` ID'lerini silin.
4. Yalnız tüm sayfalar başarıyla uygulandıktan sonra
   `last_successful_sync = response.until` yapın.

Bu akış client saatine dayanmaz. Hata durumunda `since` ilerletilmez; aynı
istek tekrarı idempotent `upsert`/`delete` olarak işlenebilir.

## Hatalar ve deployment

- `400`: bozuk veya farklı sorguya ait cursor
- `401`: API key eksik/yanlış
- `422`: geçersiz `since` veya limit
- `429`: production reverse proxy rate limit'i
- `503`: API key yapılandırılmamış ya da DB geçici olarak erişilemez

Yeni tombstone tablosu normal `python -m scripts.init_system all` shared-init
adımında oluşur. Denetimli manuel migration gerekirse:

```sh
cd backend
python -m scripts.migrate_qna_integration_deletions upgrade
```

Rollback komutu tabloyu ve birikmiş delete bilgisini sileceği için veri kaybı
etkisi vardır; yalnız entegrasyon kullanıma alınmadan önce uygulanmalıdır.

Gelecekte search gerekirse aynı router modülü altında
`GET /api/integrations/v1/search` eklenebilir. Bugün bu endpoint yoktur ve
Çözüm Merkezi'ne MeiliSearch anahtarı veya doğrudan erişim verilmez.

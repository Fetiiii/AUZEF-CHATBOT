# Docker Compose yük testi: gözlem ve olay kaydı

Bu araçlar **tek sunuculu Docker Compose test ortamı** içindir; `deploy/production/` altındaki native dağıtım araçları değildir. Preflight, watcher ve snapshot salt okunurdur. `auzef-load-run` ise **açık onayla** gerçek `/widget-chat` POST istekleri gönderir ve yeni conversation kayıtları oluşturur. Repo kökünden veya başka bir dizinden çalıştırılabilir. Gözlem araçları için Bash, Docker + Compose, `awk`, GNU `date`; generator için host üzerinde Python 3 standart kütüphanesi gerekir. DB sorgusu container içindeki `psql` ile, health kontrolü backend container'ındaki Python standart kütüphanesiyle yapılır. `jq`, `rg`, `bc` veya yeni paket gerekmez. Gözlem araçları için Docker komutlarını çalıştırma yetkisi gerekir.

## Yarın izlenecek sıra

1. Dağıtım bittikten sonra test edilen ref'i kaydedin: `git rev-parse HEAD`. `docker compose ps` ile container'ları görün. Backend image/release kimliğini operasyon notuna yazın. Test boyunca ref'i değiştirmeyin.
2. Backend recreate sonrası manuel limitlerin kaybolabileceğini kontrol edin. Beklenen **runtime** değerler: 8 GiB Memory, 12 GiB MemorySwap, 6 CPU, backend içinde 2 Uvicorn worker. Gerekirse yetkili operatör Compose'taki gerçek container adı için aşağıdaki manuel komutu uygular; scriptler bunu **asla** çalıştırmaz:

   ```bash
   sudo docker update --memory 8g --memory-swap 12g --cpus 6 auzef_backend
   ```

3. `./deploy/load-test/auzef-load-preflight` çalıştırın. **PASS** olmadan teste başlamayın. Preflight Compose servisleri `backend`, `db`, `meilisearch`, `qdrant`, `frontend`; runtime limitleri; `/health/live`, `/health/ready`; DB `SELECT 1`; cgroup sayaçlarını denetler. `ready=degraded` veya 503 başlangıç koşulu değildir.
4. Terminal 1'de `./deploy/load-test/auzef-load-watch` başlatın. Varsayılan 2 saniyeyi değiştirmek için `AUZEF_WATCH_INTERVAL=3 ./deploy/load-test/auzef-load-watch` kullanın. Terminal 2 yük üreticisi, Terminal 3 admin panel içindir. Watcher başladıktan sonra test penceresi başlar; o andan önceki backend logları sayılmaz.
5. Terminal 2'de aşağıdaki **ayrı komutlarla** smoke (2), 5, 10, 20 ve 30 eşzamanlı isteği çalıştırın. Doğru HTTPS/Nginx front-door URL'sini kullanın; HTTP→HTTPS redirect sonucunu ölçmek istemiyoruz. Her stage tamamlandıktan sonra **en az 5 saniye bekleyin** ve watcher/geçiş kapısını kontrol edin. Generator bir sonraki stage'i otomatik başlatmaz.
6. 20 ve 30 sırasında Terminal 3'te conversation list, pagination ve conversation detail'i manuel açın. Admin endpointlerine generator trafik göndermez.
7. **STOP**, `RATE_LIMITED` veya açıklanamayan **WARN** görünürse yükü artırmayın. Gerekirse generator'ı durdurun, ayrı terminalde `./deploy/load-test/auzef-load-snapshot` çalıştırın ve çıktıda verilen dizini saklayın. Snapshot, watcher'ın test başlangıcını kullanır. Watcher yoksa son 15 dakikayı kullandığını `summary.txt` içinde belirtir.
8. Her stage için aşağıdaki tabloyu doldurun. HTTP dağılımı, p50/p95/p99, wall time ve RPS **generator** çıktısından; RAM/CPU, idle transaction, QueuePool ve health **watcher**'dan; admin detail **manuel gözlemden** alınır. Watcher yalnız backend Uvicorn loglarını sayar; Nginx'in backend'e ulaşmadan ürettiği 429/503 yanıtları generator tarafından görünür.
9. Son stage sonunda snapshot alın, watcher'ı Ctrl+C ile durdurun, ölçümleri ve git ref'ini test kaydına ekleyin. Canlı sunucuda `pg_stat_activity`, QueuePool logları, istemci `/widget-chat` status dağılımı, admin detail gecikmesi, `docker stats` ve `memory.events` sonuçlarını birlikte değerlendirin.

Compose backend portu host'a yayınlamaz. Health kontrolü `docker exec` ile backend'in `127.0.0.1:8000` adresine gider. Mevcut Compose dosyasında servis adı `meilisearch`, container adı `auzef_backend` ve Uvicorn worker sayısı 2'dir. Preflight servis keşfinde sabit container adı yerine `docker compose ps -q` kullanır. Generator **frontend/Nginx** yolunu hedefler: Nginx `/widget-chat` limiti **10 istek/saniye + burst 30, nodelay**. Stage'leri aralıksız çalıştırmak 429 üretebilir; 429 bir QueuePool hatası değildir.

## Generator komutları ve sözleşmesi

Önce operatör gerçek test host'unu açıkça seçer; URL repoda saklanmaz. URL tam `/widget-chat` yolunda olmalı, credential/query/fragment içermemelidir. Generator, redirect takip etmez ve TLS sertifikasını varsayılan olarak doğrular. `--insecure` yalnız test ortamında ve bilinçli olarak sertifika doğrulamasını kapatır. Varsayılan request timeout'u **130 saniye**dir; Nginx upstream read timeout'u 120 saniyedir. Her istek bağımsız `{"message": "..."}` gönderir; conversation ID/token göndermez. İstekler round-robin sıralı sentetik [questions.txt](questions.txt) içinden seçilir; `--questions <path>` ile değiştirilebilir. Retry, otomatik stage, global HTTP bağlantı kilidi veya connection reuse yoktur: her request ayrı bağlantı açıp kapatır.

```bash
export AUZEF_LOAD_URL='https://TEST-HOST/widget-chat'

./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" --concurrency 2  --requests 2  --confirm-load-test
# Watcher kontrolü + en az 5 saniye bekleme
./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" --concurrency 5  --requests 5  --confirm-load-test
# Watcher kontrolü + en az 5 saniye bekleme
./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" --concurrency 10 --requests 10 --confirm-load-test
# Watcher kontrolü + en az 5 saniye bekleme
./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" --concurrency 20 --requests 20 --confirm-load-test
# Admin list/pagination/detail; watcher kontrolü + en az 5 saniye bekleme
./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" --concurrency 30 --requests 30 --confirm-load-test
# Admin list/pagination/detail
```

`AUZEF_LOAD_URL` yalnız `--url` verilmezse kullanılır. **`--confirm-load-test` her zaman zorunludur; interaktif prompt yoktur.** 30 üstü concurrency varsayılan olarak reddedilir; bilinçli `--allow-over-30` override'ı uyarı basar ve bu test planında kullanılmaz. Sorular dosyasında boş ve `#` ile başlayan satırlar atlanır. Başlangıç dalgası worker'lar hazır olunca tek start gate ile bırakılır; `launch_skew_ms` gerçek başlangıç farkını gösterir. `--concurrency 10 --requests 30` en çok 10 eşzamanlı istekle devam eder. RPS tek başına başarı kriteri değildir; chatbot için concurrency ve istemci gecikmesi birlikte değerlendirilir.

Çıktı varsayılan olarak `/tmp/auzef-load-test-$UID/runs/<run-id>/` altında 0700 dizin ve 0600 dosyalarla tutulur. `AUZEF_LOAD_STATE_DIR` ortak kökü, `AUZEF_LOAD_RUNS_ROOT` veya `--output-root` yalnız run kökünü değiştirir. `summary.json` ve `requests.csv` ham soru/cevap, response body, conversation token/ID veya message ID **değeri** içermez. CSV yalnız request/question indeksi, latency, HTTP status, kategori ve sözleşme boolean'larını içerir. p50/p95/p99 **nearest-rank** (`ceil(p × n) - 1`) ile yalnız HTTP 200 latency'lerinden hesaplanır. 5–30 örnekli küçük stage'lerde p95/p99 kararsızdır; p99 çoğunlukla max'a yakındır.

Generator exit kodları: **0 PASS**, **1 FAIL** (5xx, timeout, transport, redirect/diğer status veya 200 sözleşme hatası), **2 RATE_LIMITED** (yalnız 429 ve sağlam 200'ler), **3 geçersiz CLI/konfigürasyon**. PASS için tüm istekler HTTP 200, geçerli JSON, dolu `answer` ve pozitif tamsayı `message_id` içermelidir. 200 olup message ID eksikse persistence belirtisi olarak stage başarısızdır. 429 ile birlikte başka hata da varsa FAIL önceliklidir. HTTP status'ları **istemcinin gördüğü** yanıtlardır; backend loglarına dayanmaz.

| İstemci sinyali | İlk şüpheli |
| --- | --- |
| 429 | Nginx rate limit / stage pacing; backend QueuePool failure değil |
| 502 | Backend erişilemiyor / proxy |
| 503 | Backend maintenance/pool **veya** Nginx maintenance fallback; tek başına kök neden kanıtı değil |
| 504 | Upstream timeout |
| Transport timeout | Endpoint/provider aşırı gecikmesi veya ağ |
| 200 + `message_id` yok | Persistence/write problemi |
| 200 + çok yüksek latency, watcher DB temiz | Provider/embedding/CPU adayı |

## Stage geçiş kapısı ve durumlar

Bir üst stage'e yalnız şu koşullarda geçin: generator **PASS** (tüm request'ler 200 ve yanıt sözleşmesi sağlam), 429/5xx/timeout = 0; watcher'da QueuePool ve `connection timed out` log sinyali = 0; `idle in transaction >5s` = 0; live HTTP 200; cgroup OOM/OOM kill deltası 0; admin detail kullanılabilir. Her stage arasında **en az 5 saniye** bekleyin. `memory.events max` artarsa önce inceleyin. `ready=degraded`, query-log hatası, `idle in transaction >1s` veya admin gecikmesi varsa neden anlaşılmadan yükseltmeyin.

Watcher'ın kararları test başlangıcından beri kümülatiftir:

| Durum | Otomatik koşullar |
| --- | --- |
| **STOP** | Backend logunda herhangi Widget 5xx/503, QueuePool limit veya connection timeout sinyali; cgroup OOM/OOM kill artışı; live HTTP ≠ 200; `idle in transaction >5s`; backend container'ın değişmesi. 503 için havuz kaynaklı olup olmadığını ayrıca log sinyaliyle sınıflandırın. |
| **WARN** | `memory.events max` artışı; ready HTTP ≠ 200 veya `status ≠ ready` (degraded dahil); `idle in transaction >1s`; query-log yazma hatası; backend RAM ≥ %85; herhangi ölçümün okunamaması. |
| **OK** | Yukarıdaki sinyallerin hiçbiri yok. |

CPU throttling için tek bir evrensel eşik güvenilir değildir; `nr_throttled` ve `throttled_usec` başlangıç deltaları görünürdür, otomatik STOP üretmez. Log sayaçları ham satır sayısıdır; bir exception traceback'i birden fazla sinyal satırı üretebilir. PostgreSQL `active` sayısı uygulama dışı client'ları da içerebilir. DB özeti Compose `db` container'ındaki PostgreSQL cluster'ının **tüm veritabanlarını** kapsar; gelecekteki ayrı fiziksel DB-CHAT sunucusu otomatik keşfedilmez.

## Problem anında karar matrisi

| Gözlem | Hemen yapılacaklar | İlk sınıflandırma |
| --- | --- | --- |
| QueuePool timeout veya Widget 503 | Yükü artırmayın; üreticiyi durdurun; snapshot alın; `pg_stat_activity` idle transaction sürelerine bakın. Pool boyutunu rastgele artırmayın. | Connection starvation veya başka 503 nedeni; log ve istemci sonuçlarıyla ayırın. |
| `memory.events max` artıyor | Yükü artırmayın; `docker stats` RAM/limit değerini ve snapshot'ı kaydedin. Host available RAM ile cgroup limitini karıştırmayın. OOM/OOM kill varsa stage başarısızdır. | Container memory pressure. |
| CPU ve throttling artıyor, DB temiz | Snapshot alın, stage yükseltmeyin. | Embedding, thread veya CPU kapasitesi adayı; DB pool arızası olarak yazmayın. |
| DB temiz, admin detail yavaş | Snapshot alın; yük üreticisi ve admin gecikmelerini kaydedin. | Conversation COUNT/OFFSET/search/detail veya stats sorguları sonraki inceleme adayı. |
| OpenRouter yavaş, HTTP 200, uzun idle transaction ve timeout yok | Provider sürelerini decision trace/izinli ölçümlerden kaydedin. | Dış provider latency; DB incident değil. |

## Olay kaydı ve gizlilik

`auzef-load-snapshot` varsayılan olarak `/tmp/auzef-load-test-$UID/<UTC zaman>.<rastgele>/` altında 0700 izinli dizin oluşturur. `AUZEF_LOAD_SNAPSHOT_ROOT` ile kök dizin değiştirilebilir. `summary.txt`, `docker-stats.txt`, `backend-limits.txt`, `memory-events.txt`, `cpu-stat.txt`, `postgres-activity-summary.txt`, `health.txt`, `backend-errors.txt` ve `widget-statuses.txt` üretilir. `backend-errors.txt` ham log satırları değil, **yalnız zaman damgası ve operasyonel hata sınıfı** içerir. `widget-statuses.txt` test penceresi sayaçlarını içerir. Watcher'ın başlangıç zamanı `/tmp/auzef-load-test-$UID/current-start` dosyasında saklanır; gerekirse `AUZEF_LOAD_SINCE=<RFC3339>` ile snapshot penceresi seçilir. `AUZEF_LOAD_STATE_DIR` de değiştirilebilir.

Scriptler `docker inspect` içinden yalnız Memory, MemorySwap, NanoCpus, OOMKilled alanlarını seçer. DB sorguları SQL metni veya öğrenci içeriği çekmez: yalnız state, wait event, transaction yaşı, PID ve veritabanı adı alınır. Secret, connection string, cookie, conversation/verification token, öğrenci mesajı, prompt ve LLM çıktısı toplanmaz. Snapshot dizini paylaşılmadan önce yine de yerel erişim izinlerini koruyun.

## Sonuç tablosu

| Stage | Concurrency | Requests | 200 | 429 | 5xx | Timeout | p50 | p95 | p99 | Wall time | RPS | RAM peak | CPU peak | Idle-in-txn >5s | QueuePool | Health live | Admin detail |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Smoke | 2 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 1 | 5 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2 | 10 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3 | 20 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 4 | 30 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |

Concurrency, request, 200/429/5xx/timeout, p50/p95/p99, wall time ve RPS **generator**; RAM/CPU peak, idle transaction, QueuePool ve health **watcher/snapshot**; admin detail **manuel** kaynaktır. Watcher peak değerini kalıcı saklamaz; her stage sırasında görülen en yüksek değeri operatör kaydeder.

**P0.1 kabulü:** havuz kaynaklı `/widget-chat` 503 = 0; QueuePool timeout = 0; dış çağrı süresine yayılan `idle in transaction >5s` = 0; query-log pool-timeout yazma kaybı = 0; live test boyunca 200; OOM ve OOM kill = 0; `memory.events max` deltası tercihen 0; chatbot yükünde admin conversation detail kullanılabilir. Tüm stage ölçümleri kaydedilmelidir.

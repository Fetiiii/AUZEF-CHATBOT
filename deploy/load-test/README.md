# Docker Compose yük testi: gözlem ve olay kaydı

Bu araçlar **tek sunuculu Docker Compose test ortamı** içindir; `deploy/production/` altındaki native dağıtım araçları değildir. Scriptler container, DB veya uygulama durumunu değiştirmez. Repo kökünden veya başka bir dizinden çalıştırılabilir. Gerekenler: Bash, Docker + Compose, `awk`, GNU `date`; DB sorgusu container içindeki `psql` ile, health kontrolü backend container'ındaki Python standart kütüphanesiyle yapılır. `jq`, `rg`, `bc` veya yeni paket gerekmez. Docker komutlarını çalıştırma yetkisi gerekir.

## Yarın izlenecek sıra

1. Dağıtım bittikten sonra test edilen ref'i kaydedin: `git rev-parse HEAD`. `docker compose ps` ile container'ları görün. Backend image/release kimliğini operasyon notuna yazın. Test boyunca ref'i değiştirmeyin.
2. Backend recreate sonrası manuel limitlerin kaybolabileceğini kontrol edin. Beklenen **runtime** değerler: 8 GiB Memory, 12 GiB MemorySwap, 6 CPU, backend içinde 2 Uvicorn worker. Gerekirse yetkili operatör Compose'taki gerçek container adı için aşağıdaki manuel komutu uygular; scriptler bunu **asla** çalıştırmaz:

   ```bash
   sudo docker update --memory 8g --memory-swap 12g --cpus 6 auzef_backend
   ```

3. `./deploy/load-test/auzef-load-preflight` çalıştırın. **PASS** olmadan teste başlamayın. Preflight Compose servisleri `backend`, `db`, `meilisearch`, `qdrant`, `frontend`; runtime limitleri; `/health/live`, `/health/ready`; DB `SELECT 1`; cgroup sayaçlarını denetler. `ready=degraded` veya 503 başlangıç koşulu değildir.
4. Terminal 1'de `./deploy/load-test/auzef-load-watch` başlatın. Varsayılan 2 saniyeyi değiştirmek için `AUZEF_WATCH_INTERVAL=3 ./deploy/load-test/auzef-load-watch` kullanın. Terminal 2 yük üreticisi, Terminal 3 admin panel içindir. Watcher başladıktan sonra test penceresi başlar; o andan önceki backend logları sayılmaz.
5. Sırayla smoke (1–2 istek), 5, 10, 20 ve 30 eşzamanlı istek uygulayın. 20 ve 30 sırasında conversation list, pagination ve conversation detail'i açın. Her stage sonunda aşağıdaki geçiş kapısını kontrol edin; yükü doğrudan 30'a çıkarmayın.
6. **STOP** veya açıklanamayan **WARN** görünürse yükü artırmayın. Gerekirse yük üreticisini durdurun, ayrı terminalde `./deploy/load-test/auzef-load-snapshot` çalıştırın ve çıktıda verilen dizini saklayın. Snapshot, watcher'ın test başlangıcını kullanır. Watcher yoksa son 15 dakikayı kullandığını `summary.txt` içinde belirtir.
7. Her stage için aşağıdaki tabloyu doldurun. p50/p95/p99 ve gerçek istemci HTTP dağılımı **yük üreticisi** çıktısından alınır. Watcher yalnız backend Uvicorn loglarında görülen `/widget-chat` isteklerini sayar; Nginx'in backend'e ulaşmadan ürettiği 429/503 yanıtları bu sayaçta yoktur.
8. Son stage sonunda snapshot alın, watcher'ı Ctrl+C ile durdurun, ölçümleri ve git ref'ini test kaydına ekleyin. Canlı sunucuda `pg_stat_activity`, QueuePool logları, `/widget-chat` status dağılımı, admin detail gecikmesi, `docker stats` ve `memory.events` sonuçlarını birlikte değerlendirin.

Compose backend portu host'a yayınlamaz. Health kontrolü `docker exec` ile backend'in `127.0.0.1:8000` adresine gider. Mevcut Compose dosyasında servis adı `meilisearch`, container adı `auzef_backend` ve Uvicorn worker sayısı 2'dir. Preflight servis keşfinde sabit container adı yerine `docker compose ps -q` kullanır. Test trafiği frontend/Nginx üzerinden giderse oradaki chat rate limit'i de sonuca etki edebilir; 429'ları yük üreticisi raporundan ayırın.

## Stage geçiş kapısı ve durumlar

Bir üst stage'e yalnız şu koşullarda geçin: yük üreticisi ve watcher'da Widget 5xx = 0; QueuePool ve `connection timed out` log sinyali = 0; `idle in transaction >5s` = 0; live HTTP 200; cgroup OOM/OOM kill deltası 0; admin detail kullanılabilir. `memory.events max` artarsa önce inceleyin. `ready=degraded`, query-log hatası, `idle in transaction >1s` veya admin gecikmesi varsa neden anlaşılmadan yükseltmeyin.

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

| Stage | Concurrency | Requests | 200 | 5xx | p50 | p95 | p99 | Backend RAM peak | CPU peak | Idle-in-txn >5s | QueuePool timeout | Admin detail |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Smoke | 1–2 | | | | | | | | | | | |
| 1 | 5 | | | | | | | | | | | |
| 2 | 10 | | | | | | | | | | | |
| 3 | 20 | | | | | | | | | | | |
| 4 | 30 | | | | | | | | | | | |

**P0.1 kabulü:** havuz kaynaklı `/widget-chat` 503 = 0; QueuePool timeout = 0; dış çağrı süresine yayılan `idle in transaction >5s` = 0; query-log pool-timeout yazma kaybı = 0; live test boyunca 200; OOM ve OOM kill = 0; `memory.events max` deltası tercihen 0; chatbot yükünde admin conversation detail kullanılabilir. Tüm stage ölçümleri kaydedilmelidir.

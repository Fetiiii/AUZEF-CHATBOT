# AUZEF Chatbot — Performance Readiness Audit

**Audit tarihi:** 23 Eylül 2026\
**İncelenen ref:** `production-readiness`, `3183888` (çalışma ağacında audit öncesinden gelen değişiklikler de vardı)\
**Karar:** 15 eşzamanlı gerçek kullanıcı için kapasite **kanıtlanmış değil**. Bu, sistemin 15 kullanıcıyı taşıyamadığı anlamına gelmez. Mevcut kodda birden fazla makul doygunluk yolu var; kontrollü canlı yük testi henüz yapılmadı.\
**Değişiklik sınırı:** Bu audit yalnız rapordur. Runtime kodu, konfigürasyon, pipeline semantiği ve deployment değiştirilmedi.

## 1. Executive Summary

Final internal-pilot kabul koşusu 41/41 senaryo ve 44/44 turn için 0 HTTP 5xx verdi. Geliştirme makinesinde pipeline p95 4,626.93 ms idi. Bu **seri/işlevsel kabul** kanıtıdır; 15 eşzamanlı kullanıcı kapasitesi kanıtı değildir. Daha önceki immutable 14-probe semantic screen hâlâ FAIL olarak kayıtlıdır; performans audit'i bu kararı değiştirmez.

En önemli kapasite adayları: (1) iki Uvicorn worker'ın CPU üzerinde ayrı embedding modeli ve eşzamanlı encode işi taşıması, (2) senkron chat thread'lerinin dış LLM çağrılarında kalması ve uygulama düzeyi toplam deadline/backpressure bulunmaması, (3) worker başına DB pool ve thread sayısının ortak PostgreSQL kapasitesine çarpan etkisi, (4) LB/NAT arkasında Nginx IP rate limitinin gerçek kullanıcıları tek istemci sayabilmesi. Solution Center'ın bazı async uçlarında senkron DB kullanımı ayrıca event loop için bağımsız bir risktir. Hiçbirinin **15 kullanıcıda gerçekleştiği** gözlenmedi.

Yerel Compose preflight'ı servis, health, DB ve iki worker kontrollerini geçti; backend bellek limiti planlanan 8 GiB yerine 4 GiB, bellek+swap limiti 12 GiB yerine 8 GiB olduğundan **FAIL** verdi. Test planı PASS olmadan yükü yasaklıyor; bu audit yük göndermedi. Bugünkü cgroup geçmiş tepe değeri 3.94 GiB idi, fakat hangi iş yükünün oluşturduğu bilinmiyor.

## 2. Scope and Assumptions

- Kapsam: `backend/`, `chatbot-web/`, `deploy/production/`, `deploy/load-test/`, mimari ve pilot dokümanları, mevcut acceptance çıktıları ve yerel Compose'un salt okunur durumu.
- Production deployment devam ediyor. Eksik script, placeholder ve runbook tek başına finding sayılmadı. Runtime yolunu etkileyen topology/contract koşulları sayıldı.
- Kod bulguları incelenen çalışma ağacına aittir; yerel çalışan image'ın bu HEAD ve değişikliklerle birebir aynı olduğu doğrulanmadı. Yerel Compose production'ın iki APP node topolojisi değildir.
- `CONFIRMED` kod/konfigürasyon veya ölçülmüş olguyu, `LIKELY` beklenen etkisi güçlü fakat ölçülmemiş riski, `NEEDS_LOAD_TEST` eşzamanlı etkiyi, `NEEDS_RUNTIME_MEASUREMENT` kaynak nedenselliğini belirtir. `OPERATIONAL_DEPENDENCY` test veya devreye alma koşuludur. `LOW_RISK / ACCEPTED` izleme amacıyla kaydedilir.
- P0: acil ve kanıtlı durdurucu; P1: pilot öncesi doğrulama veya kontrol gerektiren yüksek risk; P2: planlı inceleme; P3: düşük öncelik. Bu audit'te kanıtlı P0 yok.

## 3. Architecture / Request Path

`Nginx /widget-chat` → FastAPI senkron `widget_chat` (AnyIO threadpool) → kullanıcı turn'ünün kısa DB oturumuyla persist edilmesi ve önceki user context'inin okunması → `answer_question` → Intent Analyzer LLM → intent başına Meili ve Qdrant retrieval (Qdrant için CPU SentenceTransformer encode), calendar/eligibility → Selector LLM veya NONE/degraded dalı → bot yanıtının kısa DB oturumuyla persist edilmesi → HTTP cevap. `chat.py:98-155,181`, `answer_pipeline.py:569-755,921-1025`, `providers.py:102-122`. MULTI en çok iki intent; dolayısıyla request başına provider/retrieval işi soru türüne göre artar. Normal/safe degraded yollar ve background query logging aynı makine kaynaklarını paylaşır.

Compose iki Uvicorn worker kullanır (`backend/entrypoint.sh:8-15`); native production topolojisi iki APP node öngörür (`docs/PRODUCTION_ARCHITECTURE.md`). Model, SDK client, circuit breaker ve bazı cache'ler process başına yaşar. DB, Meili, Qdrant ve dış LLM sağlayıcısı ortak/harici bağımlılıklardır. Aynı backend ayrıca Solution Center, admin, integration API ve health uçlarına hizmet verir; `/widget-chat` yükü bu uçların kaynaklarından bağımsız değildir.

## 4. Historical Incident Patterns Considered

Geçmiş `idle in transaction` / `ClientRead`, memory pressure, geçersiz config kaynaklı restart, Qdrant uyumluluk/auth ve model cache/cold-start olayları **yeni bulgu diye yeniden etiketlenmedi**. Bunlar testte DB session yaşı, worker RSS/cgroup, config boot doğrulaması, servis sözleşmesi ve cold-start ayrımı gerektiren hata örüntüleridir. `backend/tests/test_db_connection_lifecycle.py` sahte/izole 24-thread regresyon testi, canlı PostgreSQL kapasite testi değildir. DB lifecycle'ın ayrı çalışmada ele alınması bu audit'in DB basıncını ölçme ihtiyacını kaldırmaz.

## 5. Static Code Audit Findings

### F01 — Worker, thread, embedding ve DB pool çarpan etkisi

- **Classification / Severity / Confidence:** `NEEDS_LOAD_TEST` / P1 / HIGH (kod topolojisi), MEDIUM (15 kullanıcı etkisi).
- **Evidence:** `backend/entrypoint.sh:8-15` iki worker; `backend/routers/chat.py:174-181` senkron endpoint; `backend/services/providers.py:102-122` process içinde `SentenceTransformer` ve her Qdrant aramasında `encode()`; `docker-compose.yml:110-116` OMP/MKL 8; `backend/core/database.py:36-49` engine pool sınırlarını açıkça ayarlamıyor. SQLAlchemy varsayılanları geçerliyse ayrı admin/chat engine için worker başına pool ve overflow ayrı büyür.
- **Why / trigger:** 15+ eşzamanlı karma chat, özellikle vector retrieval ve iki intent. Düşük yükte kısa encode; yükte CPU thread oversubscription, worker RSS artışı, thread/DB wait ve p95 eğrisinde kırılma olabilir. APP-01/02 birlikte model, thread ve pool maliyetini node sayısıyla çarpar.
- **Pilot / production:** Pilot gecikmesi ve unrelated admin/health etkilenebilir; production'da iki node ortak DB/provider kapasitesini tüketebilir.
- **Validate:** Her worker için RSS, native thread sayısı, torch thread sayısı, cgroup CPU throttling, encode süresi ve `pg_stat_activity`yi 1/5/10/15/20/40 seviyelerinde eşleştir. **Direction:** Ölçümden sonra thread/process/pool bütçesini ortak kaynaklarla birlikte tasarla. **Semantic change:** NO (kaynak ayarı), fakat her ayar ayrı performans/semantic re-acceptance gerektirir.

### F02 — Provider timeout/retry ile proxy deadline bağımsız

- **Classification / Severity / Confidence:** `LIKELY` / P1 / HIGH (sınırların bağımsızlığı), MEDIUM (aktif config etkisi).
- **Evidence:** `backend/services/llm_config.py:88` timeout varsayılanı `None`; `backend/services/llm_provider.py:181-197,297-319` SDK timeout/retry yalnız konfigüre edilmişse aktarılıyor; `backend/services/ai_registry.py:375-384` timeout 1–120 sn ve retry 0–5 ayrı doğrulanıyor; `deploy/production/app/nginx/auzef-app.conf:67-74` proxy read 120 sn. Request için bütün aşamaları kapsayan deadline veya admission queue görülmedi.
- **Why / trigger:** Yavaş/429 veren provider. Tek istekte SDK beklemesi uzun sürebilir; eşzamanlılıkta senkron handler thread'leri dolar. Proxy 504 verdikten sonra backend işinin sürüp sürmediği ölçülmedi. Analyzer ve Selector ardışık olduğundan toplam bütçe tek çağrı timeout'undan büyük olabilir.
- **Pilot / production:** Kullanıcı timeout'u, ardından potansiyel gereksiz provider işi, kuyruk ve degraded oranı artışı; çok node aynı provider kotasını paylaşır.
- **Validate:** Aktif registry timeout/retry değerlerini güvenli biçimde doğrula; gecikmeli/429 test provider'ıyla proxy 504 sonrası backend active thread, SDK çağrı ve retry sayılarını izle. **Direction:** Toplam request deadline, bounded retry ve backpressure bütçesini birlikte değerlendir. **Semantic change:** YES; timeout/retry/fallback değişikliği semantic revalidation ister.

### F03 — Solution Center async handler içinde senkron DB

- **Classification / Severity / Confidence:** `CONFIRMED` (kod; yük etkisi henüz ölçülmedi) / P1 / HIGH (kod), MEDIUM (etki).
- **Evidence:** `backend/routers/solution_center.py:91` async uç; `backend/integrations/solution_center/service.py:249` çevresinde senkron `Session` query/commit; `backend/core/deps.py:278` session dependency. Async dış HTTP beklemesiyle senkron DB işleri aynı handler'da.
- **Why / trigger:** DB yavaşlığı veya SC trafiği. Düşük yükte fark edilmeyen DB beklemesi event loop'u bloklayıp diğer async uçların latency'sini artırabilir. Session'ın farklı execution context'lerde kullanılması ayrıca lifecycle gözlemi gerektirir.
- **Pilot / production:** SC kullanımı chat'e eşlik ederse aynı worker'ın event loop'u ve health etkilenebilir; iki node etkileri trafik dağılımına bağlıdır.
- **Validate:** Staging'de eşzamanlı SC verify/categories + health/chat ile event-loop lag, DB wait ve p95 ölç. **Direction:** DB işinin yürütme bağlamını lifecycle ile uyumlu hale getir. **Semantic change:** NO amaçlanır; integration davranışı regresyon testi gerekir.

### F04 — Qdrant hata nedeni görünürlüğü düşük

- **Classification / Severity / Confidence:** `CONFIRMED` (exception handling; teşhis etkisi henüz ölçülmedi) / P2 / HIGH.
- **Evidence:** `backend/services/answer_pipeline.py:313` Qdrant exception'ını availability false'a indirger; `:887` degraded dalında `except Exception: pass`. DecisionTrace kaynak availability'yi tutar fakat exception tipi/süre nedenini her dalda vermez (`docs/answer-pipeline-v2/INTERNAL_PILOT_FINAL_REACCEPTANCE_REPORT.md:187-209`).
- **Why / trigger:** Qdrant ağ/auth/versiyon hatası. Tek request cevap üretebilir; eşzamanlı kesintide bütün istekler daha zayıf candidate havuzuna ve NONE/fallback'e kayarken sebep yalnız sonuçlardan anlaşılmaz.
- **Pilot / production:** Pilot kalitesi ve hata kök nedeni ayrıştırması güçleşir. **Validate:** İzole testte Qdrant'ı geciktir/kes ve trace, log, source unavailable sayaçlarını birlikte gözle. **Direction:** PII'siz, düşük kardinaliteli hata sınıfı ve dependency latency metriği. **Semantic change:** NO.

### F05 — External client kapanışı ve config rotation

- **Classification / Severity / Confidence:** `LIKELY` / P2 / MEDIUM.
- **Evidence:** `backend/integrations/solution_center/__init__.py:18` process-global client; `base_client.py:176` AsyncClient ve `aclose()`; `backend/core/deps.py:144` dinamik OpenRouter client; `backend/services/llm_provider.py:319` Gemini client. Uygulama shutdown'da SC `aclose` çağrısı saptanmadı.
- **Why / trigger:** Uzun yaşayan worker, config/key rotation veya reload. Eski socket/transport kaynaklarının birikmesi olası; gerçek birikim ölçülmedi. Düşük yükte görünmez, uzun süreli pilotta FD/soket artışı yaratabilir.
- **Pilot / production:** Öncelikle uzun süreli kararlılık. **Validate:** Worker PID başına FD/socket sayısını config değişimi ve trafikten önce/sonra izle; kontrollü client transport testiyle close çağrılarını say. **Direction:** Client ownership ve shutdown/replacement close lifecycle'ı. **Semantic change:** NO.

### F06 — Frontend timeout sonrası tekrar gönderim idempotent değil

- **Classification / Severity / Confidence:** `CONFIRMED` (contract; sıklık henüz ölçülmedi) / P2 / HIGH.
- **Evidence:** `chatbot-web/src/widget.js:447-479` fetch için AbortController/deadline yok; `backend/routers/chat.py:98-109` kullanıcı mesajını pipeline öncesinde commit eder; widget request sözleşmesinde idempotency key yok.
- **Why / trigger:** Proxy 504, bağlantı kopması, kullanıcı tekrar denemesi. Tek request doğru; concurrency altında aynı kullanıcı turn'ü iki kez yazılabilir ve ilk iş backend'de sürüyorsa iki LLM işi çakışabilir.
- **Pilot / production:** Pilot transcript/veri kalitesi ve provider maliyeti; kullanıcıya çift yanıt. **Validate:** Geciktirilmiş test response sırasında aynı mesajı iki kez gönder; DB turn ve provider çağrılarını say. **Direction:** Turn idempotency ve istemci tekrar deneme sözleşmesi. **Semantic change:** YES; conversation davranışı yeniden kabul edilmeli.

## 6. Baseline Runtime Characteristics

Final re-acceptance: 41/41 senaryo, 44/44 turn, 0 HTTP 5xx, 87 mantıksal model çağrısı (`docs/answer-pipeline-v2/INTERNAL_PILOT_FINAL_REACCEPTANCE_REPORT.md:80-112`). Karışım SINGLE, NONE, calendar, MULTI ve context içerir; ancak seri acceptance yük eğrisi üretmez. Geliştirme host'u pipeline ortalama 3,839.83 ms, p95 4,626.93 ms, max 6,886.60 ms; Analyzer p95 2,689.40 ms, retrieval p95 566.69 ms, Selector p95 1,824.99 ms (`:211-220`). Bu değerler production SLA veya 15 concurrency baseline'ı değildir.

23 Eylül yerel Compose salt okunur örneklemi: backend iki worker, 6 CPU limiti, 4 GiB memory / 8 GiB memory+swap; backend `docker stats` yaklaşık 710 MiB idi. Worker RSS `/proc/<pid>/status` ile 672,296 ve 750,412 kB, 18'er thread; RSS toplamı paylaşılan sayfalar yüzünden cgroup kullanımı değildir. `memory.current` 1,182,728,192 bayt; `memory.peak` 4,235,476,992 bayt (3.94 GiB); `memory.events` max/oom/oom_kill sıfır; `cpu.stat` throttled 7 kez / 2,062,923 µs. Peak'in zamanı ve hangi trafikle oluştuğu bilinmiyor. Bu ölçüm boşta/çok düşük yükte tek anlıktır.

## 7. Concurrency / Capacity Findings

| Eşzamanlılık | Gerçek istek sayısı | HTTP / p50 / p95 / p99 / throughput | Kaynak korelasyonu | Sonuç |
| ---: | ---: | --- | --- | --- |
| 1 | 0 | Ölçülmedi | Ölçülmedi | Baseline acceptance seri örneği ayrı |
| 5 | 0 | Ölçülmedi | Ölçülmedi | Açık |
| 10 | 0 | Ölçülmedi | Ölçülmedi | Açık |
| **15** | **0** | **Ölçülmedi** | **Ölçülmedi** | **Kapasite kararı verilemez** |
| 20 | 0 | Ölçülmedi | Ölçülmedi | Açık |
| 40 | 0 | Ölçülmedi | Ölçülmedi | Mevcut kontrollü harness >30'u varsayılan reddeder |

`./deploy/load-test/auzef-load-preflight` 23 Eylül'de FAIL: servisler, 2 worker, live/ready ve DB `SELECT 1` PASS; memory 4/8 GiB iken plan 8/12 GiB istiyor. Bu nedenle yük çalıştırılmadı. `deploy/load-test/README.md:14-20,59-71` PASS olmadan teste başlamama ve aşama durdurma koşullarını tanımlar. Repo içinde bu planın tamamlanmış load-run summary/snapshot çıktısı bulunmadı. Var olan generator 2/5/10/20/30 stage ve kısa sentetik soru listesi sunar (`deploy/load-test/README.md:24-45`); her istekte yeni konuşma açar. Context follow-up ve çok turlu gerçek kullanıcı karışımı için ayrı workload gerekir. Küçük stage'lerin p99'u istatistiksel olarak kararsızdır.

## 8. CPU and Memory Behavior

F01'in en olası ilk node içi doygunluk adayı CPU embedding ve native thread contention'dır; bu bir **hipotezdir**. Worker başına model instance kesin, 15 kullanıcıdaki encode paralelliği ve CPU eğrisi ölçülmedi. Bugünkü iki worker RSS ve 3.94 GiB geçmiş cgroup peak'i, 4 GiB limitli Compose ortamında headroom'un geçmişte daraldığını gösterir; peak sırasında OOM/`memory.events max` yoktu. Worker çoğaltmak model kopyasını, cache/SDK state'i ve DB pool kapasitesini de çoğaltır. Production node'un gerçek limitleri ve cold-start cache durumu ayrıca doğrulanmalı. Production tasarımında `HF_HOME=/var/cache/auzef/huggingface` kalıcı cache'tir (`deploy/production/config/backend.env.example:15-16`, `deploy/production/README.md:479-493`); outbound ağ yoksa preload/erişim operasyon koşuludur. Model yükleme sırasında readiness süresi, RSS peak ve ağ/cache erişimi ayrı ölçülmelidir.

## 9. External Dependency Behavior

LLM: Analyzer ve Selector çağrıları request süresine eklenir; provider timeout/retry aktif config'e ve SDK'ya bağlı (F02). Meili/Qdrant: availability/degraded yolları var, fakat Qdrant neden ayrımı zayıf (F04). Solution Center `httpx.AsyncClient` timeout ve ağ/5xx retry kullanır (`backend/integrations/solution_center/base_client.py:176-237`); yüksek SC trafiğinde retries ortak upstream ve worker kaynaklarını artırabilir. PostgreSQL: chat kısa session düzeni mevcut olsa da admin/chat pool, list/stats ve diğer endpointler aynı cluster'ı paylaşır. Nginx: 10 istek/saniye `burst=30 nodelay` (`deploy/production/app/nginx/auzef-app.conf:4,64-65`); bu **15 eşzamanlı bağlantı** için doğrudan bir limit değildir, fakat aynı IP'den kısa sürede gelen toplam istekler 429 olabilir. Bağımlılıkların gerçek latency, hata ve kota limitleri bu audit'te ölçülmedi.

## 10. Connection / Resource Lifecycle Findings

F01 DB pool çarpanı, F03 async/sync session kullanımı, F05 client kapanışı ve F06 çift turn burada önceliklidir. Chat kullanıcı turn'ü ile bot yanıtı arasında DB session'ını uzun LLM çağrısı boyunca tutmamak üzere ayrılmıştır (`backend/routers/chat.py:98-155`); geçmiş incident'in birebir tekrarlandığı iddia edilmez. Farklı endpointlerde session ve admin sorguları ayrıca sınanmalıdır. Her worker'ın açabileceği toplam DB bağlantısı için hem pool konfigürasyonunu hem canlı `pg_stat_activity`yi iki node birlikte ölçmeden sayı taahhüdü verilemez.

## 11. Failure Propagation and Backpressure

### F07 — Admission/backpressure görünmüyor

- **Classification / Severity / Confidence:** `NEEDS_LOAD_TEST` / P1 / MEDIUM.
- **Evidence:** Senkron chat threadpool üzerinden yürür (`backend/routers/chat.py:174-181`); pipeline dış LLM ve retrieval çağrılarını inline yapar (`backend/services/answer_pipeline.py:569-755`). Nginx IP rate limit istek hızı sınırlar; in-flight chat sayısı, provider çağrısı veya embedding işi için global/node admission kontrolü saptanmadı.
- **Why / trigger:** Upstream yavaşlığı sırasında yeni request'ler. Düşük yükte her istek ilerler; eşzamanlılıkta thread kuyruğu büyüyebilir, proxy timeout ve yeniden gönderimler F06 ile yükü artırabilir. AnyIO queue wait'i mevcut trace'te ayrışmaz.
- **Pilot / production:** Latency eğrisi ani bozulabilir; admin/health aynı worker'da gecikebilir. **Validate:** 1/5/10/15/20/40 seviyelerinde in-flight, thread token/queue wait, provider in-flight ve proxy 504'ü beraber ölç. **Direction:** Ölçüm sonrası bounded admission/fail-fast tasarımı. **Semantic change:** YES; kabul/reddetme ve degraded davranışı değişir.

### F08 — Readiness bozuk search ile 200 `degraded` döner

- **Classification / Severity / Confidence:** `CONFIRMED` (contract; availability etkisi henüz ölçülmedi) / P1 / HIGH.
- **Evidence:** `backend/main.py:104-129` search sorununda `status=degraded` ile HTTP 200; `deploy/production/scripts/common.sh:204-214` `ready|degraded` kabul eder. `backend/services/providers.py:47-55,102-110` health çağrıları senkron provider client üzerinden.
- **Why / trigger:** Meili/Qdrant yavaş veya ulaşılamaz. Health yeşil görünen node degraded cevap üretebilir; sık/uzun health çağrısı yavaş dependency'ye takılabilir. Gerçek LB sağlık politikası ayrıca doğrulanmalı.
- **Pilot / production:** Pilot kalite kaybı availability alarmından önce başlayabilir; iki node aynı dependency'yi paylaşıyorsa her ikisi birlikte etkilenir. **Validate:** İzole fault injection ile ready status/latency, route-to-node ve chat degraded oranını ölç. **Direction:** Liveness, readiness ve quality-degraded alarmlarını operasyon sözleşmesinde ayır. **Semantic change:** NO, health routing değişirse operasyonel davranış değişir.

Circuit breaker state'i process bazlıdır (`backend/services/circuit_breaker.py:9`); APP-01/02 aynı provider arızasını ayrı ayrı öğrenir. Bu, aynı arıza için kısa süreli ek çağrı ve node'lar arasında farklı degraded yanıtlar doğurabilir; **NEEDS_LOAD_TEST**, P2. Retry storm kanıtı yok, fakat F02/F07 ve SC retry birleşimi fault injection ile test edilmelidir.

## 12. Observability Gaps

Final pilotta 44/44 normal ve 4/4 failure turn için DecisionTrace doğrulandı; intent/retrieval/selector süreleri, source availability, NONE/degraded nedenleri var (`docs/answer-pipeline-v2/INTERNAL_PILOT_FINAL_REACCEPTANCE_REPORT.md:187-209`). Bu önemli bir temel sağlar. Aşağıdaki sorulara tek trace ile cevap verilemiyor veya production ölçümü gösterilmedi:

| Soru | Eksik korelasyon |
| --- | --- |
| İstek girişte kuyruklandı mı? | Nginx upstream timing, ASGI receive → handler başlangıcı, AnyIO token wait |
| CPU/embedding mi doygun? | Worker bazlı encode süreleri, CPU throttling, RSS/native thread, cgroup memory events |
| DB mi bekledi? | Pool checkout wait, transaction yaşı, query/commit süreleri; `pg_stat_activity` snapshot |
| Hangi dış servis? | Meili/Qdrant network latency ve hata sınıfı, provider attempt/retry ve rate-limit; Qdrant F04 |
| Nginx/LB mi? | Gerçek client IP, node başına 429/502/504, upstream response ve request süreleri |
| Uzun süreli sızıntı mı? | Worker PID bazlı FD/socket, model RSS, cgroup memory.current/peak ve restart korelasyonu |

DecisionTrace'te provider metadata/retry alanları bulunur, ancak SDK'nın gerçekten yaptığı retry sayısı `backend/services/llm_provider.py:211-212` nedeniyle `None` olabilir. Background query log ve trace yazımı yükte ayrı ölçülmedi; logging'in önemli bir bottleneck olduğu **iddia edilmiyor**. Ham kullanıcı/prompt/secret loglamadan düşük kardinaliteli sayaç ve histogram gerekir.

## 13. Confirmed Bottlenecks

**15 kullanıcıda doğrulanmış bottleneck yok.** Doğrulanmış olgular: worker başına embedding modeli, senkron chat, timeout/proxy bütçelerinin ayrı olması, async SC içinde senkron DB, readiness'in degraded 200 sözleşmesi, Nginx IP rate limit ve bu ortamda load preflight FAIL. Bunlar risk mekanizmasıdır; saturation point değildir. 41/41 acceptance sonucu kapasiteyi doğrulamaz.

## 14. Suspected Risks Requiring Measurement

F01, F02, F05, F07 ve aşağıdaki LB/shared-resource koşulları ölçüm ister. İlk saturation point için henüz FastAPI, threadpool, CPU, RAM, DB, Meili, Qdrant, provider veya Nginx arasında kanıtlı sıralama yapılamaz. İlk deney aynı anda istemci latency, worker CPU/RSS/thread, DB wait ve provider/retrieval sürelerini toplamalı; tek başına HTTP p95 kök neden değildir.

## 15. Operational Dependencies Relevant to Runtime

### F09 — LB/NAT gerçek IP ve node-local rate limit

- **Classification / Severity / Confidence:** `OPERATIONAL_DEPENDENCY` / P1 / MEDIUM.
- **Evidence:** `deploy/production/app/nginx/auzef-app.conf:4-7,18-34,64-65` rate zone `$binary_remote_addr` ile, trusted LB CIDR konfigürasyonuna bağlı. Node-local Nginx zone APP-01 ve APP-02 arasında paylaşılmaz. `deploy/production/README.md:347-364` LB IP restore gereksinimini belirtir.
- **Why / trigger:** LB veya kampüs NAT tüm pilot kullanıcıları aynı `$remote_addr` altında gösterirse ortak 10 r/s + burst 30 bütçesi false 429 üretir; iki node aynı istemciye farklı toplam limit uygular. 15 eşzamanlı kullanıcı tek başına 429 kanıtı değildir; geliş hızı ve IP eşlemesi belirleyicidir.
- **Pilot / production:** Pilot soruları sistem kapasitesi sanılarak 429 olabilir; production node dağılımında tutarsızlık. **Validate:** LB üzerinden farklı istemci IP'leriyle `$remote_addr`, `X-Forwarded-For`, backend client IP ve node bazlı 429 sayaçlarını karşılaştır. **Direction:** Trusted proxy ve rate-limit anahtar sözleşmesini canlı topolojiye göre doğrula. **Semantic change:** NO; kabul edilen trafik davranışı değişir.

### F10 — Preflight kaynak limiti bu testte sağlanmadı

- **Classification / Severity / Confidence:** `OPERATIONAL_DEPENDENCY` / P1 / HIGH.
- **Evidence:** 23 Eylül `auzef-load-preflight`: backend memory 4 GiB, memory+swap 8 GiB; beklenen 8/12 GiB; diğer servis, 6 CPU, 2 worker, live/ready, DB PASS. `deploy/load-test/README.md:8-18,59-71` test geçiş koşulları.
- **Why / trigger:** Düşük limitli koşu üretim kapasitesini temsil etmez ve geçmiş 3.94 GiB peak'e yakın çalışır. **Pilot / production:** Yanlış kapasite kararı veya test sırasında memory pressure. **Validate:** Yetkili test ortamında hedef limitleri, image/ref'i ve kaynak headroom'unu doğrulayıp preflight PASS al. **Direction:** Hedef topolojiyle eşlenmiş, aşamalı test. **Semantic change:** NO.

## 16. Risks Explicitly Ignored Because Deployment Is Incomplete

Eksik rollback/runbook, placeholder trusted LB CIDR'nin **salt placeholder olması**, tamamlanmamış deploy script ayrıntıları ve henüz yayımlanmamış production artifact'leri bağımsız finding sayılmadı. CIDR'nin gerçek client-IP restore edilmemesi F09'da koşullu runtime riski olarak ele alındı. Local Compose ile native production paketlerinin farklı olması da tek başına kusur sayılmadı; kapasite sonuçlarının birbirine genellenmesini engelleyen sınırdır.

## 17. Internal Pilot Risk Assessment

**15 kullanıcı riskli görünüyor mu?** Evet, doğrulanmamış fakat makul P1 kapasite yolları var; **15'in başarısız olacağını söyleyecek veri yok**. İlk saturation point ve subsystem bilinmiyor. Tek request'te masum görünüp kaynak biriktirebilecek işler worker model/threads, provider bekleme, client socket lifecycle ve timeout sonrası duplicate work. İki APP node DB/provider/search ortak bağımlılıklarını paylaşır, node-local cache/circuit/rate-limit state'leri ise ayrıdır. Dış bağımlılık yavaşladığında safe fallback semantiği var; thread/queue ve toplam deadline bakımından kontrollü kapasite sonlanması gösterilmedi. Pilot öncesi P0 yok; P1 olarak F01/F02/F07/F09/F10'un ölçüm/konfigürasyon kapıları ve F03'ün karma trafik testi gerekir.

Pilot sırasında öncelikli metrikler: istemci 200/429/502/503/504/timeout ve p50/p95/p99; source/intent türüne göre degraded/NONE; Nginx upstream timing ve node/IP; active/queued requests ve thread token; worker PID RSS/FD/thread/CPU, cgroup current/peak/events/throttling; embedding süreleri; DB pool wait, `pg_stat_activity` state/age; Meili/Qdrant latency/hata; provider attempt, timeout, rate limit ve circuit state. Admin conversation list/detail gecikmesi aynı anda izlenmeli.

## 18. Prioritized Findings

| ID | Öncelik | Sınıf | Kanıtın izin verdiği karar |
| --- | --- | --- | --- |
| F01 | P1 | NEEDS_LOAD_TEST | Model/thread/pool çarpanı var; 15 kullanıcı etkisi bilinmiyor |
| F02 | P1 | LIKELY | Deadline uyumsuzluğu mümkün; aktif config ve fault test gerekli |
| F03 | P1 | CONFIRMED | Async SC yolunda senkron DB var; event-loop etkisi ölçülmeli |
| F07 | P1 | NEEDS_LOAD_TEST | In-flight backpressure gösterilmedi |
| F08 | P1 | CONFIRMED | Degraded 200 readiness sözleşmesi var |
| F09 | P1 | OPERATIONAL_DEPENDENCY | LB gerçek IP doğrulanmalı |
| F10 | P1 | OPERATIONAL_DEPENDENCY | Yerel load preflight FAIL |
| F04 | P2 | CONFIRMED | Qdrant hata nedeni izleme boşluğu |
| F05 | P2 | LIKELY | Client close/FD growth ölçülmeli |
| F06 | P2 | CONFIRMED | Retry duplicate turn üretebilir |

## 19. Safe Before Internal Pilot

Davranışı değiştirmeden: hedef node/ref ve kaynak limitlerini kaydet; preflight PASS al; gerçek LB client-IP/rate-limit eşlemesini test et; aktif provider timeout/retry ayarlarını ve SDK retry gözlemini kaydet; PII'siz watcher/snapshot ile worker PID RSS/CPU/thread/FD, cgroup, DB, Nginx ve provider metriklerini ilişkilendir; küçük bir cold-start ve dependency fault dry-run yap. Mevcut DecisionTrace'i koru. Bunlar audit önerileridir, bu görevde uygulanmadı.

## 20. Defer Until After Internal Pilot

Yalnız performans skorunu iyileştirmek için response/semantic answer cache, LLM bypass veya routing shortcut önermiyoruz: pilotun gerçek pipeline verisini bozabilir. Ölçüm olmadan worker/pool/thread sayısı ve parallel retrieval değişikliği de ertelenmeli. Admin liste ölçeklemesi, uzun süreli client FD eğilimi ve eski sekme lazy chunk problemi (`deploy/production/scripts/auzef-deploy:275-280`, `chatbot-web/src/app/app.routes.ts:14-28`) izleme sonucuna göre planlanabilir.

## 21. Requires Semantic Revalidation

Timeout/retry bütçesi, admission/backpressure, fail-fast, degraded seçimi, cache/LLM bypass, idempotency/turn işleme ve retrieval paralelliği kullanıcıya dönen sonuç veya sıralamayı değiştirebilir. Bunlardan biri sonradan uygulanırsa freeze kuralları, 14-probe screen ve 41 senaryolu acceptance politikası ayrıca değerlendirilmelidir. Bu audit hiçbirini uygulamadı.

## 22. Recommended Next Investigations

1. Test sunucusunda hedef image/ref, 8/12 GiB ve 6 CPU/2 worker gibi planlanan limitleri doğrula; preflight PASS olmadan yük gönderme. APP-01/02 production topolojisinde limitler farklıysa testi o değerlerle yeniden tasarla.
2. Nginx front door üzerinden warm-up ve steady-state'i ayıran gerçekçi karışımla 1, 5, 10, **15**, 20; güvenli ise 40 concurrency uygula. Her sanal kullanıcıya ayrı conversation/token ver. SINGLE SELECT, expected NONE, Calendar, context follow-up, MULTI ve fallback/suggestion yolları yer alsın. Mevcut generator bağımsız tek turn gönderdiği için bu karışımı tek başına temsil etmez.
3. Her stage için en az birkaç tekrar ve yeterli örnekle request count/success/status, RPS, p50/p95/p99/max, timeout, degraded ve provider failure say; warm-up'ı ayrı tut. 5–20 örnekli p99'u kapasite eşiği sayma. STOP/WARN kapısını uygula; 429 veya 5xx geldiğinde aşama yükseltme.
4. Aynı zaman damgasında Nginx → ASGI queue → Analyzer → retrieval/embedding → Selector → DB persist zamanlarını, worker RSS/CPU/thread/FD, cgroup ve `pg_stat_activity` ile birleştir. İlk eğri kırılmasını kaynak doygunluğuyla ilişkilendir.
5. Kontrollü fault injection ile yavaş/429 LLM, Meili/Qdrant kesintisi, DB pool beklemesi ve SC timeout'u dene. Proxy timeout sonrası backend işinin sürmesi, retry sayısı, circuit state, readiness ve admin yanıtı ölçülsün. Pilot semantiği değiştirilmesin.
6. Tek node sonucu APP-01 + APP-02'ye genelleme. İki node testinde toplam DB bağlantısı, provider kota paylaşımı, node-local circuit/cache ve LB IP/rate limit ayrıca doğrulansın.

**Son karar sınırı:** Bu rapor 15 kullanıcı kapasitesini onaylamaz veya reddetmez. İlk güvenilir karar, preflight PASS olan hedef topolojide gerçekçi workload ve eşzamanlı kaynak korelasyonuyla verilebilir.

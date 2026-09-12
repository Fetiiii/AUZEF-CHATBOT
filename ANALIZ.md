# AUZEF Akıllı Asistan — Test Öncesi Sistem Analizi

> **Amaç:** Sistemin kurum sunucusunda test aşamasına geçmeden önce kapatılması gereken
> eksiklerin tespiti.
> **Yöntem:** Kod tabanının tamamı okundu (backend katmanları, nginx/Docker
> konfigürasyonu, widget, Angular yapısı, test paketi, DEPLOY.md, README.md).
> **Kapsam dışı:** Bu belge yalnızca tespittir; hiçbir kod değiştirilmemiştir.
>
> Her bulgu **Kanıt → Etki → Öneri** biçimindedir. Kanıt sütununda dosya ve satır
> numarası verilmiştir; doğrulanamayan hiçbir iddia rapora alınmamıştır.

---

## 1. Yönetici Özeti

Proje mühendislik olarak olgun: katmanlı mimari SPEC'e sadık, sır yönetimi disiplinli
(TC ve OTP hiçbir yerde saklanmıyor), performans iyileştirmeleri ölçüme dayalı ve
gerekçelendirilmiş, kapsamlı bir backend test paketi gerçek veritabanına karşı
koşuyor. Sorun *eksik
özellik* değil, **canlıya çıkışta kırılacak operasyonel ve güvenlik boşlukları**.

Test aşamasına çıkışı bloklayan **7 madde** tespit edildi; **P0-1 çözüldü**, 6'sı açık:

| # | Bulgu | Tür |
|---|---|---|
| ~~P0-1~~ | ~~Çözüm Merkezi uçlarında hiç hız sınırı yok~~ → ✅ **ÇÖZÜLDÜ** | Güvenlik |
| P0-2 | OTP deneme sayacı yok → doğrulama kodu brute-force'a açık | Güvenlik |
| P0-3 | Admin girişinde brute-force koruması pratikte etkisiz (10 istek/saniye) | Güvenlik |
| P0-4 | `/api/search` public ve limitsiz → LLM bütçesi dışarıdan tüketilebilir | Güvenlik / Maliyet |
| P0-5 | SSL sertifikası yerleşimi hiçbir yerde yazılı değil → nginx ayağa kalkmaz | Operasyon |
| P0-6 | Embedding modeli imaja gömülü değil, açılışta internetten iniyor | Operasyon |
| P0-7 | KVKK aydınlatma metni / açık rıza adımı yok (TC + IP işleniyor) | Uyum |

Bunlardan **P0-5, P0-6 ve P0-7 kurum sunucusuna erişim veya kurumsal karar gerektirir**;
geliştirici tarafından lokalde tek başına kapatılamaz. Diğerleri kod ve konfigürasyon
değişikliğiyle çözülebilir.

Ayrıca **P1-18 (akademik takvimin ilk yükleme yolunun dokümante olmaması)** bloklayıcı
sayılmasa da testin *anlamlılığını* doğrudan etkiler: takvim tablosu boş kalırsa tarih
soruları hata vermeden yanlış cevaplanır. Test öncesi mutlaka doğrulanmalıdır.

---

## 2. Sistem Genel Bakış

### 2.1 Mimari

```
Tarayıcı / gömülü widget
        │
        ▼
nginx (frontend container)  ── TLS sonlandırma, SPA servisi,
        │                       hız sınırı, bakım bayrağı, güvenlik başlıkları
        ▼
FastAPI (backend container) ── 2 uvicorn worker, sync endpoint'ler threadpool'da
        ├── PostgreSQL   — QnA, konuşmalar, kullanıcılar, takvim, SC oturumları
        ├── MeiliSearch  — anahtar kelime araması (circuit breaker'lı)
        ├── Qdrant       — semantik vektör araması (BGE-M3 Türkçe embedding)
        ├── LLM          — OpenRouter / OpenAI / Gemini (yalnızca SEÇİCİ rolünde)
        └── Çözüm Merkezi API — talep oluşturma (httpx.AsyncClient)
```

Dahili servislerin hiçbiri dış dünyaya açık değil (`expose`, `ports` değil); backend
bile host'a yayınlanmıyor. Tek dış kapı nginx. Bu doğru bir tercih.

### 2.2 Cevap üretim zinciri

[`backend/services/answer_pipeline.py`](backend/services/answer_pipeline.py)

LLM açıkken:
1. Soru alt sorulara bölünür (`split_questions`).
2. Her alt soru için aday havuzu kurulur: **tüm takvim kayıtları** (havuzun başına) +
   Qdrant'tan 8 + MeiliSearch'ten 5 QnA adayı, cevaba göre tekilleştirilmiş.
3. LLM havuzdan **birebir seçim** yapar — kendi cevabını asla üretmez, yalnızca bir
   numara döner ([`llm_provider.py:38-54`](backend/services/llm_provider.py#L38)).
4. "Böl" ve "tek soruymuş gibi seç" adımları **paralel** çalıştırılır; soru tek çıkarsa
   spekülatif seçim kullanılır (tek soruda ~yarı gecikme).

LLM kapalı veya erişilemezse eşik yedeği devreye girer: takvim kelime eşleşmesi →
MeiliSearch skoru ≥ 0.90 → Qdrant skoru > 0.75.

**Tasarım açısından doğru bir karar:** LLM'in kurum bilgisi uydurması yapısal olarak
engellenmiş; halüsinasyon riski aday havuzuyla sınırlanmış.

### 2.3 Çözüm Merkezi entegrasyonu

[`backend/integrations/solution_center/`](backend/integrations/solution_center/) —
SPEC.md'deki katmanlı yapı birebir uygulanmış: `base_client` (transport) / `client`
(HTTP çağrıları) / `service` (iş mantığı) / `mapper` / `category_cache` / `exceptions` /
`models`. İş mantığı client'a, HTTP çağrısı service'e sızmamış.

Durum makinesi veritabanında (`solution_center_sessions`), sahiplik ayrı bir token icat
edilmeden `conversations.client_token` üzerinden doğrulanıyor.

### 2.4 Yetkilendirme

Veritabanı oturumlu HttpOnly cookie (JWT değil — anında iptal edilebilsin diye), token
veritabanında SHA-256 hash'iyle, parolalar bcrypt ile. Üç seviyeli rol:
`editor < admin < super_admin`. Middleware fail-closed: bilinmeyen rol seviye 0 alır.

---

## 3. Sistemin Güçlü Yanları

Bunlar raporun dengesi için değil, **korunması gereken kararlar** oldukları için yazıldı:

1. **Sır yönetimi disiplinli.** `verificationToken` frontend'e hiçbir zaman dönmüyor;
   TC ve OTP hiçbir yerde saklanmıyor; `httpx`/`httpcore` logger'ları modül yüklenirken
   WARNING'e sabitlenmiş ([`base_client.py:47`](backend/integrations/solution_center/base_client.py#L47))
   — "loglanmayacak" kuralının pratikte en çok ihlal edildiği yer tam olarak burasıdır
   ve önlem alınmış.

2. **Konuşma sahiplik token'ı.** `conversations.id` ardışık olduğu için token olmadan
   herkes başkasının konuşmasına yazabilir/puanlayabilirdi; `client_token` bunu
   kapatıyor ve eski (token'sız) kayıtlara yazma fail-closed reddediliyor
   ([`chat.py:38-45`](backend/routers/chat.py#L38)).

3. **Performans işi ölçüme dayalı.** pg_trgm GIN partial index, keyset (seek)
   sayfalama, toplu encode, streaming CSV — hepsi kod yorumlarında ölçüm sonucuyla
   gerekçelendirilmiş ([`core/database.py:228-237`](backend/core/database.py#L228),
   [`conversations.py:41-48`](backend/routers/conversations.py#L41)).

4. **Bakım modu üç katmanlı ve backend'den bağımsız.** Panel düğmesi / nginx'in 502
   yakalaması / `bakim.sh`. Bayrak dosya olduğu için backend çökükken bile çalışıyor.

5. **CSV formül enjeksiyonu düşünülmüş** ([`csv_utils.py:8`](backend/services/csv_utils.py#L8))
   — kullanıcı mesajı Excel'de formül olarak çalışamıyor.

6. **Test paketi ciddi.** Raporun yazıldığı anda 61 test (P0 düzeltmeleriyle
   birlikte artmaya devam ediyor), gerçek Postgres'e karşı, gerçek middleware ile;
   arama/embedding sağlayıcıları stub'lanmış (ağ/model gerekmiyor).

---

## 4. Bulgular

### P0 — Test aşamasına çıkmadan kapatılmalı

---

#### P0-1 · Çözüm Merkezi uçlarında hiç hız sınırı yok — ✅ ÇÖZÜLDÜ

> **DURUM: KAPATILDI.** Üç katmanlı hız sınırı uygulandı ve uçtan uca doğrulandı.
> Uygulama ayrıntısı bu bölümün sonundadır. Aşağıdaki analiz, çözümün neden bu
> şekilde tasarlandığını göstermek için korunmuştur.

**Kanıt (düzeltme öncesi)**

- [`chatbot-web/nginx.conf:91`](chatbot-web/nginx.conf#L91) — `location /api/` bloğunda
  `limit_req` **yok**. Tüm konfigürasyonda hız sınırı yalnızca iki yerde:
  `location = /widget-chat` ([satır 57](chatbot-web/nginx.conf#L57)) ve
  `location = /api/auth/login` ([satır 77](chatbot-web/nginx.conf#L77)).
- `/api/solution-center/send-sms` public bir uç
  ([`routers/solution_center.py:91`](backend/routers/solution_center.py#L91)) ve
  `/api/` bloğundan geçiyor → sınırsız.
- Tek gereksinim geçerli bir `conversation_token`; o da tek bir `/widget-chat`
  çağrısıyla bedava alınıyor ([`chat.py:58`](backend/routers/chat.py#L58)).
- Servis katmanında da sayaç yok
  ([`service.py:102 start_verification`](backend/integrations/solution_center/service.py#L102)).

**Etki**

1. **SMS bombardımanı.** Herhangi bir T.C. kimlik numarasına sınırsız doğrulama SMS'i
   gönderilebilir. Kuruma doğrudan SMS maliyeti çıkar; hedef alınan vatandaş taciz edilir.
2. **TC doğrulama oracle'ı.** Uç, kayıtlı olmayan TC için hata, kayıtlı TC için
   `maskedPhoneNumber` döner ([`service.py:108-114`](backend/integrations/solution_center/service.py#L108)).
   Yani sistem "bu TC AUZEF öğrencisi mi?" sorusuna sınırsız cevap veren bir servise
   dönüşür — üstelik telefonun son 3 hanesini de sızdırarak.

**Uygulanan çözüm**

Üç katman; hiçbiri diğerinin yerini tutmaz:

| Katman | Ne yapar | Nerede |
|---|---|---|
| 1 · nginx | Kaba, durumsuz IP sınırı — betikle saldırıyı backend'e hiç ulaştırmaz | [`nginx.conf`](chatbot-web/nginx.conf) `sc_sms_limit` bölgesi, 20 istek/dk + burst 10, yalnızca `= /api/solution-center/send-sms` exact-match location'ında |
| 2 · backend | Anlamsal sayaçlar — asıl savunma | [`rate_limit.py`](backend/integrations/solution_center/rate_limit.py) `SmsRateLimiter` |
| 3 · gizlilik | TC sayaç anahtarı `HMAC-SHA256`; ham TC yazılmaz | aynı dosya, `_secret()` / `_key()` |

**Eşikler** ("Dengeli" profil, hepsi `.env`'den ayarlanabilir —
[`.env.example`](.env.example) `CM_SMS_*`):

| Kapsam | Limit | Pencere | Hangi saldırıyı keser |
|---|---|---|---|
| Konuşma | 3 SMS | 15 dk | Tek oturumdan hamle |
| TC | 5 SMS | 60 dk | **SMS bombardımanı** (farklı konuşmalardan aynı numaraya) |
| IP | 30 SMS | 60 dk | **TC numaralandırma** (her TC bir kez denendiği için TC sayacı bunu görmez) |

**Tasarım kararları**

- **Sayaçlar DB'de, bellekte değil** — uvicorn 2 worker ile çalışıyor
  ([entrypoint.sh:15](backend/entrypoint.sh#L15)); süreç-içi sayaç gerçek limiti ikiye katlardı.
- **Artırma tek atomik `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`** — iki worker
  aynı satıra yazarken SELECT-sonra-UPDATE yarışa açıktı.
- **Sayaç CM çağrısından ÖNCE tüketilir** ve **başarısız TC denemeleri de sayılır** —
  numaralandırma zaten başarısız aramalarla yapılıyor.
- **Hata mesajı hangi kapsamın dolduğunu söylemez** — "bu TC için limit doldu" demek
  yeni bir oracle açardı.
- **TC asla saklanmaz**: anahtar `HMAC-SHA256(secret, TC)`. Secret `SC_HASH_SECRET`'tan,
  yoksa `CM_SERVICE_TOKEN`'dan türetilir — rastgele üretilemez, çünkü her worker farklı
  secret üretir ve TC sayacı ayrışırdı.

**Doğrulama sonuçları**

| Kontrol | Sonuç |
|---|---|
| Birim testleri ([`test_sc_rate_limit.py`](backend/tests/test_sc_rate_limit.py), 7 senaryo) | ✅ geçti |
| Tam paket regresyonu | ✅ **76 test geçti** (önceki 61 + yeniler) |
| Backend limiti uçtan uca | ✅ 429 + Türkçe mesaj, **CM'ye 0 çağrı** (log sayacıyla kanıtlandı) |
| nginx limiti uçtan uca | ✅ ilk 11 istek geçti, 12–25 arası 429; gövde JSON + `Retry-After: 60` |
| Diğer SC uçları etkilenmedi | ✅ `categories` sınırsız kaldı (exact-match yalnızca `send-sms`) |
| Ham TC sızıntısı | ✅ tabloda 11 haneli sayı yok, tüm anahtarlar 64 karakter hex |
| Log sızıntısı | ✅ log yalnızca `kapsam=conv conversation_id=8` yazıyor; TC/hash yok |

**Kalan artık risk**

Dağıtık saldırı (çok sayıda farklı IP) uygulama katmanında çözülmez — kurumun WAF/CDN
katmanı gerekir. Ayrıca oracle *tamamen* kapatılmadı: maskeli telefonu göstermek akışın
gereği. Hedef oracle'ı yok etmek değil, sınırsızdan saatte birkaç denemeye indirmekti.

---

#### P0-2 · OTP deneme sayacı yok

**Kanıt**

- [`service.py:143 verify_otp`](backend/integrations/solution_center/service.py#L143) —
  yanlış kodda yalnızca `InvalidOtpException` fırlatılıyor; deneme sayılmıyor, oturum
  kilitlenmiyor, `verification_token` geçersizleştirilmiyor.
- [`core/database.py:118 SolutionCenterSession`](backend/core/database.py#L118) —
  tabloda `attempt_count` benzeri bir kolon yok.

**Etki**

6 haneli doğrulama kodu = 1.000.000 kombinasyon. P0-1'deki hız sınırı boşluğuyla
birleştiğinde, `verificationToken`'ın 10 dakikalık geçerlilik süresi
([`constants.py:41`](backend/integrations/solution_center/constants.py#L41)) içinde
kombinasyon uzayının anlamlı bir kısmı denenebilir. Başarılı olan saldırgan
**başkasının adına kuruma resmî talep açar**.

**Öneri**

`solution_center_sessions` tablosuna `attempt_count` kolonu; 5 yanlış denemeden sonra
`verification_token = NULL` + state sıfırlama. Kullanıcıya "çok fazla hatalı deneme,
lütfen baştan başlayın" mesajı.

---

#### P0-3 · Admin girişinde brute-force koruması pratikte etkisiz

**Kanıt**

- [`nginx.conf:77`](chatbot-web/nginx.conf#L77) — `/api/auth/login`, sohbet için
  tanımlanmış `chat_limit` bölgesini kullanıyor:
  [`rate=10r/s`](chatbot-web/nginx.conf#L5).
- [`admin/auth.py:145 login`](backend/admin/auth.py#L145) — başarısız deneme sayacı ya
  da hesap kilidi yok.

**Etki**

IP başına saniyede 10 parola denemesi ≈ günde 864.000 deneme. Konfigürasyondaki yorum
bu bloğu "parola deneme saldırısına karşı sıkı rate limit" olarak tanımlıyor
([satır 74-75](chatbot-web/nginx.conf#L74)) ama seçilen oran sohbet trafiği için
ayarlanmış; parola koruması için fiilen hiçbir engel yok.

**Not:** Bu bulgu, giriş ucunun geri kalanının *yanlış* olduğu anlamına gelmiyor.
Timing-attack koruması (kullanıcı yokken de bcrypt çalıştırılıyor,
[satır 151](backend/admin/auth.py#L151)) ve pasif hesap ile yanlış parolanın aynı 401
mesajını dönmesi doğru yapılmış. Eksik olan yalnızca deneme *hızı*.

**Öneri**

- Ayrı bir bölge: `rate=5r/m`, `burst=5`.
- Backend'de N başarısız denemeden sonra hesap ya da IP için geçici kilit (ör. 10 deneme
  → 15 dakika). Kilit sayacı DB'de tutulmalı ki 2 worker arasında paylaşılsın.

---

#### P0-4 · `/api/search` public ve limitsiz

**Kanıt**

- [`routers/chat.py:195`](backend/routers/chat.py#L195) — `GET /api/search`.
- [`admin/auth.py:197 _ROLE_RULES`](backend/admin/auth.py#L197) — korunan desenler
  `settings|conversations|stats|qna|academic-calendar`. `/api/search` hiçbirine
  uymuyor → `required_role_for` `None` döner → **public**.
  (`tests/test_routing.py::test_search_is_public_and_routed` bunu bilerek doğruluyor.)
- Uç, `/widget-chat` ile **aynı** `_answer_question` pipeline'ını çağırıyor
  ([satır 202](backend/routers/chat.py#L202)) — yani embedding hesabı + iki LLM çağrısı.
- Ama `/widget-chat` hız sınırlıyken ([`nginx.conf:57`](chatbot-web/nginx.conf#L57)),
  `/api/search` limitsiz `/api/` bloğundan geçiyor.

**Etki**

Hız sınırı `/widget-chat` üzerine konmuş, fakat aynı maliyetli işi yapan ikinci bir kapı
açık kalmış. Bir betik `/api/search`'ü döngüde çağırarak LLM kredisini tüketebilir ve
embedding CPU'sunu doyurarak gerçek öğrencilerin sorularını yavaşlatabilir.

**Öneri**

Önce frontend'in bu ucu gerçekten kullanıp kullanmadığı kontrol edilmeli. Kullanmıyorsa
kaldırılmalı; kullanıyorsa `/widget-chat` ile aynı hız sınırı bölgesine alınmalı.

---

#### P0-5 · SSL sertifikası yerleşimi hiçbir yerde belgelenmemiş

> ⚠️ **Kurum sunucusu gerektirir** — geliştirici lokalde kapatamaz.

**Kanıt**

- [`docker-compose.yml:158`](docker-compose.yml#L158) —
  `- ./ssl/certs:/etc/nginx/ssl:ro` mount ediliyor.
- [`nginx.conf:19-20`](chatbot-web/nginx.conf#L19) — `fullchain.pem` ve `privkey.pem`
  dosyaları bekleniyor.
- Repoda `ssl/` klasörü yok. Bu **doğru** (`.gitignore`'da, sertifika repoya girmemeli).
- Ancak `DEPLOY.md` ve `README.md`'de "bu dosyaları şuraya koy" adımı **yok**. Aramada
  SSL'e dair tek geçen ifade, geliştirme bölümündeki "SSL sertifikası gerekmez" notu
  ([`DEPLOY.md:197`](DEPLOY.md#L197)).

**Etki**

Temiz bir sunucuda `docker compose up -d` çalıştırıldığında Docker `./ssl/certs`'ü boş
klasör olarak oluşturur, nginx sertifika dosyasını bulamaz ve frontend container'ı
crash-loop'a girer. Site hiç açılmaz. Backend sağlıklı olduğu için sorunun nereden
geldiği ilk bakışta anlaşılmaz.

**Öneri**

`DEPLOY.md`'ye "İlk Kurulum" bölümü: klasör yolu, beklenen dosya adları, dosya izinleri,
ve sertifika yenilendikten sonra `docker compose restart frontend` gerektiği notu.
Ayrıca `nginx.conf`'taki `server_name auzefasistan.istanbul.edu.tr` sabit olduğu için,
farklı bir test alan adı kullanılacaksa bunun da güncellenmesi gerektiği yazılmalı.

---

#### P0-6 · Embedding modeli imaja gömülü değil, açılışta internetten iniyor

> ⚠️ **Kurum sunucusunun dış ağ erişimine bağlı** — lokalde sorunsuz çalışması bu riski gizler.

**Kanıt**

- [`core/deps.py:36`](backend/core/deps.py#L36) — `QDRANT_PROVIDER` modül **import
  anında** kuruluyor.
- [`services/providers.py:52`](backend/services/providers.py#L52) — yapıcı içinde
  `SentenceTransformer(model_name)` → model yoksa HuggingFace'ten indiriliyor
  (`nezahatkorkmaz/turkce-embedding-bge-m3`, ~1.5 GB).
- [`entrypoint.sh:15`](backend/entrypoint.sh#L15) — `--workers 2`, yani model **her
  worker için ayrı** yükleniyor.
- [`docker-compose.yml:129-133`](docker-compose.yml#L129) — healthcheck
  `start_period: 180s`, ardından 5 × 15s.
- [`docker-compose.yml:162-164`](docker-compose.yml#L162) — frontend
  `depends_on: backend: condition: service_healthy`.

**Etki**

1. İlk açılışta ~1.5 GB indirme 180 saniyeyi aşarsa container `unhealthy` işaretlenir →
   restart → indirme baştan başlar. Döngüye girilebilir.
2. Frontend backend'in sağlıklı olmasını beklediği için, bu süre boyunca **site hiç
   açılmaz** (bakım mesajı bile görünmez, çünkü nginx henüz ayakta değildir).
3. Kurum sunucusunda dış internet erişimi kısıtlı veya proxy arkasındaysa (kamu
   kurumlarında sık) boot **tamamen** başarısız olur ve bu ancak deploy sırasında
   fark edilir.

**Öneri**

Modeli Docker build aşamasında imaja indirmek (`Dockerfile`'a bir `RUN` adımı) en
sağlam çözüm — build makinesi internete çıkar, çalışma zamanı çıkmaz. Alternatif olarak
`hf_cache` volume'u deploy öncesi doldurulur. Her iki durumda da `start_period`
yükseltilmeli ve `DEPLOY.md`'ye "ilk açılış N dakika sürer" notu eklenmeli.

---

#### P0-7 · KVKK aydınlatma metni ve açık rıza adımı yok

> ⚠️ **Kurumsal karar gerektirir** — hukuk/KVKK birimiyle koordinasyon.

**Kanıt**

- [`chatbot-web/src/widget.js:756`](chatbot-web/src/widget.js#L756) — talep sihirbazı
  doğrudan "Lütfen T.C. Kimlik numaranızı girin" diyerek başlıyor; öncesinde aydınlatma
  metni, onay kutusu veya bilgilendirme yok.
- [`core/database.py:75`](backend/core/database.py#L75) ve
  [`:86`](backend/core/database.py#L86) — `query_logs.ip_address` ve
  `conversations.ip_address` saklanıyor.
- Kod tabanında saklama süresi, otomatik silme veya anonimleştirme mekanizması yok.

**Etki**

T.C. kimlik numarası özel nitelikli olmasa da kimlik belirleyici kişisel veridir; IP
adresi de kişisel veridir. Kamu üniversitesi için işleme faaliyetinin aydınlatma
yükümlülüğü ve saklama süresi politikası olmadan yürütülmesi bir uyum riskidir. Bu
teknik bir hata değil, **test öncesi kurumla netleşmesi gereken bir eksiktir**.

**Kayda değer olumlu taraf:** Sistem yalnızca *gerekli olanı* işliyor — TC ve OTP
hiçbir yerde saklanmıyor, `verificationToken` istemciye hiç gönderilmiyor
([`core/database.py:113-117`](backend/core/database.py#L113) yorumu bu kararı açıkça
belgeliyor). Yani veri minimizasyonu zaten uygulanmış; eksik olan yalnızca
bilgilendirme/rıza katmanı ve saklama politikası.

**Öneri**

- SC akışının ilk adımına aydınlatma metni bağlantısı + onay kutusu.
- `query_logs` ve `conversations` için saklama süresi kararı (ör. 12 ay) ve süresi dolan
  kayıtlarda IP alanının anonimleştirilmesi.

---

### P1 — Test sırasında sorun çıkaracak

---

#### P1-8 · Çözüm Merkezi entegrasyonu — kısmen doğrulandı, kalan risk daraldı

> ✅ **GÜNCELLEME:** Lokal kurulumda canlı API'ye **SMS göndermeyen** doğrulama
> yapıldı ve aşağıdaki beş riskten **üçü kapandı**. Ayrıntı bu bölümün sonunda.

`.env` içindeki `CM_SERVICE_TOKEN` dolu. Kodda canlıda kırılabilecek **beş nokta**
tanımlanmıştı:

| # | Risk | Kanıt | Durum |
|---|---|---|---|
| 1 | Auth şeması yanlış olabilir | [`constants.py:20`](backend/integrations/solution_center/constants.py#L20) — `DEFAULT_AUTH_SCHEME = "Api-Key"`, SPEC.md ise `Bearer` diyor | ✅ **`Api-Key` DOĞRU** — canlı API 200 döndü |
| 2 | Yanıt zarfı farklı olabilir | [`service.py:41 _ok()`](backend/integrations/solution_center/service.py#L41) | ✅ Kategori ucunda doğru çözümlendi |
| 3 | Mapper flat→tree dönüşümü | [`mapper.py`](backend/integrations/solution_center/mapper.py) | ✅ 90 düğümlük ağaç doğru kuruldu |
| 4 | Kanal kodu yanlış olabilir | [`.env.example:39`](.env.example#L39) — SPEC'te "örnek" olarak verilmiş | ⚠️ **HÂLÂ AÇIK** |
| 5 | `Student`/`TicketResult` alias'ları | Pydantic modelleri | ⚠️ **HÂLÂ AÇIK** (yalnızca OTP sonrası test edilebilir) |

**Yapılan doğrulama (SMS gönderilmedi, TC istenmedi)**

`/api/solution-center/categories` ucu geçerli bir konuşma token'ıyla çağrıldı. Bu uç
TC/SMS gerektirmez ama **aynı service token'ı ve base URL'i kullanır**:

```
HTTP 200
INFO:auzef.sc:SC istek endpoint=/service/chatbotapi/v1/tum-kategorileri-al
              status=200 sure=113ms
```

Gelen canlı kategori ağacı:

| | |
|---|---|
| Kök kategori | 4 — *Ders Materyalleri, Sistemlere Giriş Problemleri, Sınavlar, Öğrenci İşleri* |
| Toplam düğüm | 90 |
| Seçilebilir (`isLeaf=true`) | 65 |
| Ağaç derinliği | 5 |

Bu sonuç `CM_BASE_URL`, `CM_SERVICE_TOKEN` ve `CM_AUTH_SCHEME`'in **üçünü birden**
doğruluyor. **SPEC.md'deki `Bearer` bilgisi yanlış, koddaki `Api-Key` doğrudur** —
SPEC güncellenmelidir (bkz. P2-16).

**Loglama disiplini de ampirik olarak doğrulandı:** log satırı yalnızca endpoint +
status + süre içeriyor; gövde yok. `.env`'deki service token değeri backend
loglarının tamamında arandı — **hiçbir yerde geçmiyor**. SPEC'in "Service Token
loglanmayacak" kuralı pratikte tutuyor.

**Kalan iş**

1. `CM_CHANNEL_SHORTCODE`'un doğru değeri kurumdan **yazılı olarak** teyit edilmeli.
   Bu hata yalnızca kullanıcı TC girip SMS alıp OTP doğrulayıp kategori seçtikten
   **sonra**, yani en pahalı noktada ortaya çıkar.
2. Kurumun vereceği test TC'siyle uçtan uca akış denenmeli (öğrenci listesi ve ticket
   yanıtının Pydantic modelleriyle eşleştiğinin tek doğrulama yolu budur).

---

#### P1-8b · Kategori ağacı SPEC'in varsaydığından çok daha derin — talep akışı UX'i gözden geçirilmeli

**Kanıt**

Canlı API'den gelen ağaçta seçilebilir kategorilere ulaşma maliyeti:

| Tıklama sayısı | Kategori adedi |
|---|---|
| 2 | 4 |
| 3 | 41 |
| 4 | 20 |

En derin örnek yol:
`Ders Materyalleri > Açık Öğretim > Ders Kitabı > Ders Kitabı Eksikliği/Görüntüleme Hatası`

Buna karşılık [`SPEC.md:588-608`](SPEC.md#L588) örneği **iki seviyeli** bir ağaç
varsayıyor (*Öğrenci İşleri → Yeni Kayıt*).

**Değerlendirme**

Widget'ta **kod hatası yok**: `scCategoryPicker` yığın tabanlı drill-down ve "‹ Geri"
düğmesiyle keyfi derinliği doğru işliyor
([`widget.js:866-903`](chatbot-web/src/widget.js#L866)).

Ancak sohbet balonu içinde, 65 seçenek arasından doğru olanı bulmak için öğrencinin
3–4 kez dallanması ve yanlış dala girerse geri dönmesi gerekiyor. Arama/filtre alanı
yok. Bu bir kusur değil, **testte gözlenmesi gereken bir kullanılabilirlik riskidir**:
öğrenci talep akışını burada terk ederse bunun ölçülebilir olması gerekir.

**Öneri**

Test sırasında kategori adımının tamamlanma oranı gözlenmeli. Terk oranı yüksekse
seçenek listesine yazarak filtreleme eklenmesi (ağaç zaten tek çağrıda istemciye
geldiği için sunucu değişikliği gerektirmez).

---

#### P1-9 · CI yok; test paketi elle çalıştırılıyor, frontend'de hiç test yok

**Kanıt**

- Repoda `.github/` klasörü yok — hiçbir otomatik tetikleyici yok.
- [`tests/conftest.py:28`](backend/tests/conftest.py#L28) — testler kendi geçici
  Postgres container'ını açıyor, yani Docker'a bağımlı.
- [`chatbot-web/package.json`](chatbot-web/package.json) `"test": "ng test"` tanımlıyor,
  ancak `angular.json`'da `test` target'ı **yok** (yalnızca `build` ve `serve`) → komut
  hata verir. Frontend'de sıfır test var.

**Etki**

Kapsamlı bir backend test paketi mevcut (rapor yazılırken 61 test) ama koşulması
geliştiricinin hatırlamasına bağlı. Regresyonlar
deploy sırasında yakalanıyor. `DEPLOY.md` "backend'e dokunan her değişiklikten sonra
suite'i çalıştırın" diyor — bu bir sürecin yerini tutmaz.

**Öneri**

GitHub Actions ile push/PR'da `pytest` koşan basit bir workflow. `services: postgres`
kullanılırsa `conftest.py`'nin Docker bağımlılığı da devre dışı kalır
(`TEST_DATABASE_URL` zaten destekleniyor).

---

#### P1-10 · Veritabanı şema değişiklikleri Alembic yerine elle SQL ile

**Kanıt**

[`core/database.py:202-238`](backend/core/database.py#L202) — her açılışta idempotent
`CREATE INDEX IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` listesi
çalıştırılıyor.

**Değerlendirme**

Bu yaklaşım şu an **çalışıyor** ve her satır yorumla gerekçelendirilmiş — küçümsenecek
bir çözüm değil. Ancak geri alma (downgrade) yok ve kolon tipi değişimi veya veri
taşıması gerektiren bir migration'da bu yöntem kırılır.

Test aşaması için **bloklayıcı değildir**; projeyi devralacak kişi için teknik borç
olarak kaydedilmelidir.

---

#### P1-11 · İzleme ve uyarı katmanı yok

**Kanıt**

- [`main.py:79`](backend/main.py#L79) — `/health` yalnızca `SELECT 1` çalıştırıyor.
  MeiliSearch, Qdrant ve LLM sağlayıcısının durumu kontrol edilmiyor.
- [`core/deps.py:48 meili_search_safe`](backend/core/deps.py#L48) — MeiliSearch çökerse
  circuit breaker boş liste döndürüp hatayı yutuyor.

**Etki**

MeiliSearch veya Qdrant çöktüğünde sistem `healthy` görünmeye devam eder; kullanıcı
cevap almaya devam eder ama **cevap kalitesi sessizce düşer** (anahtar kelime araması
veya semantik arama devre dışıyken LLM'in aday havuzu daralır). Bu, fark edilmesi en zor
arıza türüdür.

Dış uptime izleme, hata toplama (Sentry vb.) ve log toplama da yok. `DEPLOY.md` bunu
kabul ediyor ("ayrı bir iştir").

**Öneri (asgari)**

`/health` yanına `/health/deep` eklenmesi: DB + MeiliSearch + Qdrant durumunu ayrı ayrı
raporlayan, kurum izleme sistemine verilebilecek bir uç. Container healthcheck'i mevcut
ucuz `/health`'te kalmalı.

---

#### P1-12 · Yedekleme otomatik değil

**Kanıt**

[`DEPLOY.md:17-19`](DEPLOY.md#L17) — `pg_dump` elle çalıştırılan bir adım olarak
öneriliyor ("Önerilen"). Cron veya zamanlanmış görev yok, geri yükleme tatbikatı
yapılmamış. MeiliSearch ve Qdrant volume'ları hiç yedeklenmiyor.

**Değerlendirme**

Meili ve Qdrant verisi PostgreSQL'den yeniden üretilebilir
([`scripts/vector_sync.py`](backend/scripts/vector_sync.py),
[`scripts/importer.py`](backend/scripts/importer.py)), dolayısıyla asıl kritik olan
PostgreSQL. Ancak "yeniden üretilebilir" olması sürecin yazılı olduğu anlamına gelmiyor
— felaket anında bu iki komutun sırası ve süresi bilinmiyor.

**Öneri**

Sunucuda günlük `pg_dump` cron'u + saklama politikası; `DEPLOY.md`'ye "tam kurtarma"
prosedürü (DB geri yükle → `importer` → `vector_sync`).

---

#### P1-18 · Akademik takvimin ilk yükleme yolu dokümante değil → tarih soruları sessizce yanlış cevaplanır

**Kanıt**

- `data/` altında iki CSV hazır: `module_479_Qna.csv` ve
  `auzef_akademik_takvim_2025_2026_guncel.csv`. Klasör backend'e mount ediliyor
  ([`docker-compose.yml:114`](docker-compose.yml#L114)).
- [`scripts/importer.py:83`](backend/scripts/importer.py#L83) — varsayılan dosya
  `data/module_479_Qna.csv`; script **yalnızca QnA** yüklüyor. Akademik takvim için
  hiçbir CLI script'i yok.
- Takvimin tek yükleme yolu panel ucu
  ([`routers/calendar.py:145`](backend/routers/calendar.py#L145)), o da giriş yapmış
  bir `editor` gerektiriyor.
- [`README.md:65-75`](README.md#L65) "İlk Veri Yükleme" bölümü yalnızca
  `scripts.importer` ve `scripts.vector_sync` komutlarını veriyor — ne takvim
  yüklemesinden ne de panele girebilmek için gereken `scripts.create_admin`'den
  söz ediyor.

**Etki**

README'deki adımlar harfiyen izlenirse `academic_calendar` tablosu **boş kalır**.
Bunun sonucu sessizdir: [`answer_pipeline.py:133`](backend/services/answer_pipeline.py#L133)
aday havuzuna tüm takvim kayıtlarını ekler; tablo boşsa havuza takvim adayı girmez ve
"final ne zaman", "bütünleme tarihleri" gibi sorular MeiliSearch/Qdrant'a düşer. Oradan
büyük ihtimalle *"sınav tarihleri akademik takvimde yayımlanır"* türü genel bir QnA
seçilir. **Sistem hata vermez, sadece somut tarih yerine yönlendirme cevabı verir.**

Tarih soruları öğrencilerin en sık sorduğu soru tipi olduğu için, bu durum testin
tamamını yanıltıcı hâle getirebilir: değerlendiren kişi "bot takvimi bilmiyor" sonucuna
varır, oysa sorun yalnızca verinin hiç yüklenmemiş olmasıdır.

**İyi haber:** CSV formatı import ucunun beklediğiyle **birebir uyuyor** —
başlık `Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi`, uç ayracı otomatik algılıyor
([`calendar.py:161-163`](backend/routers/calendar.py#L161)) ve Türkçe sütun adlarını
tanıyor ([`calendar.py:170-173`](backend/routers/calendar.py#L170)).
Dosya hazır; eksik olan yalnızca yükleme yolunun yazılı olmaması.

**Öneri**

`README.md`'nin ilk veri yükleme bölümü doğru sıraya çekilmeli:

1. `docker compose exec backend python -m scripts.importer` (QnA → PostgreSQL + Meili)
2. `docker compose exec backend python -m scripts.vector_sync` (Qdrant vektörleri)
3. `docker exec -it auzef_backend python -m scripts.create_admin <e-posta> --name "..."`
   (ilk super_admin — panele girebilmek için)
4. Panel → Akademik Takvim → CSV içe aktar →
   `data/auzef_akademik_takvim_2025_2026_guncel.csv`

Daha sağlamı: takvim için de bir CLI script'i eklenmesi (mevcut import mantığı
`calendar.py`'de zaten var, tekrar yazılması gerekmez), böylece ilk kurulum panele
bağımlı olmaktan çıkar.

**Doğrulama:** kurulum sonrası takvim tablosunun dolduğu, bota "güz dönemi final
sınavları ne zaman" sorularak teyit edilmeli — somut tarih dönmeli.

---

#### P1-19 · `scripts/importer.py` idempotent değil → iki kez çalıştırılırsa QnA verisi ikiye katlanır

**Kanıt**

[`scripts/importer.py:19`](backend/scripts/importer.py#L19) —
`INSERT INTO qna (question_text, answer_text, status) VALUES (:q, :a, 1) RETURNING id`.
`ON CONFLICT` yok, öncesinde `TRUNCATE`/`DELETE` yok, CSV'deki `qna_id` sütunu
kullanılmıyor (kimlik veritabanı tarafından yeniden üretiliyor).

Karşılaştırma: aynı script'te `tags` ve `qna_tags` tabloları `ON CONFLICT` ile korunmuş
([satır 29](backend/scripts/importer.py#L29), [35](backend/scripts/importer.py#L35))
— yani koruma bilinen bir teknik, yalnızca ana tabloya uygulanmamış.

**Etki**

Script ikinci kez çalıştırıldığında (bağlantı koptu, "acaba yüklendi mi" diye tekrar
denendi, ya da yeniden kurulum yapıldı) tüm QnA kayıtları **ikinci kez eklenir**. Yeni
kayıtlar yeni id aldığı için MeiliSearch ve Qdrant'a da ayrı doküman olarak gider →
arama sonuçlarında çift kayıt, LLM aday havuzunda aynı cevabın iki kopyası.

Tekilleştirme cevaba göre yapıldığından ([`answer_pipeline.py:98-105`](backend/services/answer_pipeline.py#L98))
kullanıcı çift cevap görmez; ama arama sonuçları ve `/api/qna` listesi kirlenir,
istatistikler bozulur ve temizlik elle yapılır. Test aşamasında bu komutun birden fazla
kez çalıştırılması oldukça olası.

**Öneri**

Kısa vadede `README`/`DEPLOY`'a "bu komut yalnızca boş veritabanında bir kez
çalıştırılır" uyarısı. Kalıcı çözüm: CSV'deki `qna_id` kimlik olarak kullanılıp
`ON CONFLICT (id) DO UPDATE`, ya da script başında mevcut kayıtların temizlenmesi
(açık onay isteyerek).

---

#### P1-20 · Python bağımlılıklarının hiçbiri sürüm sabitlenmemiş — uyumsuzluk şimdiden oluştu

**Kanıt**

- [`backend/requirements.txt`](backend/requirements.txt) — 16 paketin **hiçbirinde**
  sürüm kısıtı yok (`fastapi`, `sqlalchemy`, `pandas`, `sentence-transformers`,
  `qdrant-client`, `openai`, `pydantic`, `bcrypt` … hepsi çıplak isim).
- Buna karşılık altyapı imajları **sabitlenmiş**: `postgres:15`,
  `getmeili/meilisearch:v1.12` ([`docker-compose.yml:50`](docker-compose.yml#L50)),
  `qdrant/qdrant:v1.13.2` ([`docker-compose.yml:74`](docker-compose.yml#L74)),
  `nginx:1.27-alpine`.
- **Sonuç şimdiden gözlendi.** Lokal kurulumda backend açılışında:

  ```
  UserWarning: Qdrant client version 1.18.0 is incompatible with server
  version 1.13.2. Major versions should match and minor version difference
  must not exceed 1.
  ```

  Kurulan `qdrant-client` 1.18.0; sunucu sabitlenmiş 1.13.2. Aradaki fark istemcinin
  kendi uyumluluk kuralını 5 minor sürüm aşıyor.

**Etki**

1. **Yeniden üretilebilirlik yok.** Bugün build edilen imaj ile üç ay sonra build
   edilen imaj farklı bağımlılık kümesi içerir. "Lokalde çalışıyordu, sunucuda
   çalışmıyor" sınıfı hataların en yaygın kaynağıdır ve teşhisi zordur.
2. **Sessiz kırılma riski.** Qdrant uyarısı şu an yalnızca uyarı; ancak istemci/sunucu
   protokolü ayrıştığında arama sonuçları bozulabilir veya çağrılar hata verebilir.
   Vektör araması `try/except` içinde çağrıldığı için
   ([`answer_pipeline.py:92-94`](backend/services/answer_pipeline.py#L92),
   [`:196-201`](backend/services/answer_pipeline.py#L196)) hata **yutulur** ve sistem
   semantik arama olmadan çalışmaya devam eder — kimse fark etmez.
3. Rollback güvenilirliği düşer: `DEPLOY.md`'deki imaj etiketi ile geri dönüş yalnızca
   eski imaj hâlâ diskteyse çalışır; yeniden build gerekirse aynı sürümler gelmez.

**Öneri**

- `requirements.txt` sabitlenmeli. En pratik yol: çalışan bir konteynerde
  `pip freeze > requirements.lock.txt` alıp bunu imaj build'inde kullanmak.
- `qdrant-client` özellikle sunucu sürümüyle uyumlu bir sürüme çekilmeli
  (`qdrant-client>=1.13,<1.15`) **ya da** compose'daki Qdrant imajı yükseltilmeli.
  İkisinden biri seçilip birlikte güncellenmeli — bu ikisi çifttir.

---

#### P1-21 · Yeni kurulum LLM kapalı geliyor ve bu hâliyle bazı soruları yanlış cevaplıyor

> Bu bulgu, sistemin lokalde ayağa kaldırılıp **canlı olarak denenmesi** sırasında
> gözlendi; teorik bir çıkarım değildir.

**Kanıt — iki parça**

**(a) LLM varsayılan olarak kapalı ve açılması gerektiği hiçbir yerde yazmıyor.**
[`scripts/init_system.py:15`](backend/scripts/init_system.py#L15) — ilk kurulumda
`LLM_ENABLED` değeri `"false"` olarak yazılıyor. Açmanın tek yolu Ayarlar sayfası
(yalnızca super_admin). Ne `README.md` ne `DEPLOY.md` kurulum adımları arasında bunu
anıyor. `.env`'de geçerli bir `OPENROUTER_API_KEY` bulunsa bile sistem LLM'i
kullanmaz — anahtarın varlığı yetmez, DB'deki bayrak da açılmalıdır
([`core/deps.py:116-122`](backend/core/deps.py#L116)).

**(b) Eşik yedeğindeki takvim kapısı "nasıl" sorularında yanlış tetikleniyor.**
[`answer_pipeline.py:29-33`](backend/services/answer_pipeline.py#L29) — `is_date_query`,
sorunun *zaman* soruyor olup olmadığına bakmadan, metinde `event_keywords` listesinden
herhangi bir kelime geçiyorsa `True` dönüyor. `"kayıt yenileme"` bu listede
([satır 30](backend/services/answer_pipeline.py#L30)). Dolayısıyla
*"kayıt yenileme **nasıl** yapılır"* sorusu tarih sorusu sayılıyor ve takvim kapısı
açılıyor ([`:187-190`](backend/services/answer_pipeline.py#L187)).

**Gözlenen davranış (aynı soru, aynı veri, tek fark LLM bayrağı):**

| Soru | LLM kapalı (kurulum varsayılanı) | LLM açık |
|---|---|---|
| "Kayıt yenileme nasıl yapılır" | ❌ *"Güz Dönemi — Kayıtlı Öğrenciler İçin Kayıt Yenileme: 01.09.2025 – 21.09.2025 tarihleri arasındadır."* | ✅ Doğru prosedür cevabı (ödeme + ders alım işlemleri) |
| "Güz dönemi final sınavları ne zaman" | ✅ Doğru tarih | ✅ Doğru tarih |
| "Bütünleme ne zaman ve harç ücretini nereden ödeyebilirim" | — | ✅ İki alt soru ayrılıp **iki cevap birleştirilmiş** |

**Etki**

Kurulum talimatları harfiyen izlendiğinde sistem, mimarisinin en zayıf modunda çalışır:
LLM seçici devre dışıdır ve kelime tabanlı takvim kapısı prosedür sorularını tarih
cevabıyla karşılar. Bunu fark etmek için birinin Ayarlar sayfasına girip LLM'i açması
gerekir — ama böyle bir adım hiçbir belgede yok.

Kod, bu riski LLM **açık** yol için zaten çözmüş: LLM "uygun aday yok" dediğinde takvim
kapısı bilinçli olarak yeniden açılmıyor
([`answer_pipeline.py:179-186`](backend/services/answer_pipeline.py#L179) yorumu tam
olarak bu senaryoyu anlatıyor). Eksik olan, aynı korumanın LLM **kapalı** yolda
bulunmaması.

**Öneri**

1. Kurulum belgelerine "Ayarlar → LLM'i aç" adımı eklenmeli; ya da `init_system.py`
   geçerli bir API anahtarı varsa `LLM_ENABLED`'ı `true` başlatmalı.
2. `is_date_query`, olay kelimesinin yanında bir *zaman göstergesi* de araması için
   daraltılmalı; ya da soru "nasıl / nereden / kim / neden" ile başlıyorsa takvim
   kapısı açılmamalı.

---

### P2 — Temizlik ve dokümantasyon

---

#### P2-13 · README güncelliğini yitirmiş

Aşağıdakiler `README.md` üzerinde tek tek doğrulandı:

| Satır | Yazan | Gerçek |
|---|---|---|
| [83](README.md#L83) | `http://localhost/chat` → "Öğrenci arayüzü" | `/chat` route'u `chatbot/sign-in`'e redirect ediyor ([`app.routes.ts`](chatbot-web/src/app/app.routes.ts)); sohbet artık **yalnızca widget** |
| [84](README.md#L84) | Admin paneli girişi **"(fb/1)"** | Gerçek kullanıcı + rol sistemi var; bu bilgi hem yanlış hem kötü örnek |
| [116-129](README.md#L116) | Docker'sız lokal geliştirme adımları | `MEILI_URL`, `QDRANT_HOST`, `QDRANT_PORT`, `DATABASE_URL` hiç geçmiyor — `.env.example`'da da yok |
| [180](README.md#L180) | `requirements.txt` kök dizinde | `backend/requirements.txt` altında |
| [71](README.md#L71) | `python -m scripts.importer` ile CSV yükleme | Panelden CSV import da var; hangisi kanonik belirsiz |

**Hiç geçmeyen bölümler:** Çözüm Merkezi entegrasyonu, rol sistemi
(editor/admin/super_admin), bakım modu ve `bakim.sh`, HTTPS/nginx yapılandırması,
`docker-compose.dev.yml`, test paketi.

**En kritik alt madde:** [`core/deps.py:30`](backend/core/deps.py#L30) `MEILI_URL`'i
**varsayılansız** `os.getenv` ile okuyor. Docker içinde compose bu değişkeni veriyor
([`docker-compose.yml:107`](docker-compose.yml#L107)), ama README'nin Docker'sız
geliştirme akışı harfiyen izlenirse `MeiliSearchProvider` `None` bir URL ile kurulmaya
çalışır. Lokalde geliştirme yapılacağı için bu madde doğrudan geliştiriciyi etkiler.

**Lokal geliştirme için `.env`'e eklenmesi gerekenler:**

```env
DATABASE_URL=postgresql://admin:<parola>@localhost:5432/auzef_bot
MEILI_URL=http://localhost:7700
QDRANT_HOST=localhost
QDRANT_PORT=6333
ADMIN_COOKIE_SECURE=false
```

**Bunları eklemek Docker'ı bozmaz.** Dört değişkenin dördü de compose'un `environment:`
bloğunda tanımlı ([`docker-compose.yml:106-109`](docker-compose.yml#L106)) ve Docker
Compose'da `environment:`, `env_file:`i **ezer** — konteyner içinde compose'un değeri
(`db`, `meilisearch`, `qdrant` servis adları) kazanmaya devam eder. `.env`'deki
`localhost` değerleri yalnızca backend Docker dışında çalıştırıldığında devreye girer.

**`MEILI_MASTER_KEY` ise doğru yerde:** o değişken `environment:` bloğunda **yok**,
dolayısıyla hem MeiliSearch konteyneri ([`docker-compose.yml:54`](docker-compose.yml#L54))
hem backend onu `.env`'den almak zorunda. `.env`'de bulunması gereklidir ve mevcuttur.

---

#### P2-14 · `.gitignore` ile gerçek durum çelişiyor

**Kanıt**

`.gitignore` `data/`, `docker-compose.dev.yml` ve `.vscode/settings.json` girdilerini
içeriyor; ancak `git ls-files` üçünün de **takipte** olduğunu gösteriyor (ignore
kuralları, dosyalar zaten takibe alındıktan sonra eklenmiş).

**Etki**

Bloklayıcı değil, ama niyet ile davranış ayrışmış. `docker-compose.dev.yml`'in takipte
olması aslında **iyi** (`DEPLOY.md` onu kullanmayı öneriyor, klonlayan kişinin alması
gerekir) — bu durumda yanlış olan `.gitignore` girdisidir, dosya değil.

**Öneri**

`.gitignore`'dan `docker-compose.dev.yml` çıkarılmalı. `data/` ve
`.vscode/settings.json` için kasıtlı olarak takipte kalıp kalmayacaklarına karar
verilip `.gitignore` buna göre düzeltilmeli.

---

#### P2-15 · Ölü kod: sabit IP içeren `constants.ts`

**Kanıt**

[`chatbot-web/src/app/services/services/shared/constants.ts:5`](chatbot-web/src/app/services/services/shared/constants.ts#L5) —
`apiUrl = 'https://161.9.141.143:8080'`. Tüm frontend'de arandı: bu sabit **hiçbir
yerde kullanılmıyor**. Gerçek API çağrıları
[`api-credentials.interceptor.ts`](chatbot-web/src/app/services/services/api-credentials.interceptor.ts)
üzerinden göreli `/api` yoluna gidiyor.

**Etki**

İşlevsel etkisi yok, ancak repoda gereksiz bir iç ağ IP'si bırakıyor ve okuyanı yanıltıyor.

**Öneri**

Dosya silinmeli.

---

#### P2-16 · SPEC.md geliştirme checklist'i tamamen işaretsiz

[`SPEC.md:706-719`](SPEC.md#L706) — 12 maddelik "Geliştirme Checklist'i"nin hiçbiri
işaretli değil. Oysa kod incelemesinde **on ikisinin de** uygulandığı görülüyor
(BaseClient, authorization, Pydantic modelleri, client metodları, service katmanı,
mapper, memory cache, exception yapısı, state machine, ticket akışı, loglama, unit
testler).

Belge güncellenmezse iş yarım kalmış izlenimi verir — özellikle projeyi dışarıdan
değerlendirecek biri için yanıltıcı.

Ayrıca [`SPEC.md:188`](SPEC.md#L188) `Bearer` şemasını tarif ediyor ama kodda
`Api-Key` kullanılıyor ([`constants.py:20`](backend/integrations/solution_center/constants.py#L20));
kod yorumu bunun canlı OpenAPI ile doğrulandığını söylüyor. SPEC'e düzeltme notu
düşülmeli (bkz. P1-8).

---

#### P2-17 · Test fixture'ında eksik tablo temizliği — ✅ ÇÖZÜLDÜ

**Tespit edilen (rapor yazıldığında):** `clean_tables` fixture'ının `TRUNCATE`
listesinde `academic_calendar` yoktu; takvim kayıtları testler arasında
sızabiliyordu.

**Durum:** P0-1 çalışmasında kapatıldı — `academic_calendar` ve yeni eklenen
`sc_rate_limits` birlikte listeye alındı
([`tests/conftest.py`](backend/tests/conftest.py) `clean_tables`). Sayaç
tablolarının listede olmaması testleri sıraya bağlı hâle getireceği için bu,
sonraki P0'larda da tekrarlanan bir kontrol maddesi oldu.

(`solution_center_sessions` listede yok ama `conversations` üzerinden `CASCADE` ile
temizleniyor — bu tarafta sorun yok.)

---

## 5. Test Aşamasına Geçiş Kontrol Listesi

**Koda dokunularak kapatılabilir (lokalde yapılabilir):**

- [x] ~~Çözüm Merkezi uçları için ayrı nginx hız sınırı bölgesi~~ — **tamamlandı** (P0-1)
- [x] ~~Backend'de SMS gönderim sayacı — konuşma / TC / IP~~ — **tamamlandı**, 76 test geçti (P0-1)
- [ ] `solution_center_sessions.attempt_count` + 5 denemede oturum iptali (P0-2)
- [ ] `/api/auth/login` için ayrı hız sınırı bölgesi + hesap kilidi (P0-3)
- [ ] `/api/search` kapatılması veya hız sınırına alınması (P0-4)

**Kurum sunucusu / kurumsal karar gerektirir:**

- [ ] SSL sertifikası yerleşiminin `DEPLOY.md`'ye yazılması ve sunucuda doğrulanması (P0-5)
- [ ] Embedding modelinin imaja gömülmesi + `start_period` yükseltilmesi (P0-6)
- [ ] Sunucunun HuggingFace'e (veya bir aynaya) erişiminin teyidi (P0-6)
- [ ] KVKK aydınlatma metni + rıza adımı; IP saklama süresi kararı (P0-7)
- [x] ~~CM auth şeması / base URL / service token doğrulaması~~ — **tamamlandı**,
      `Api-Key` doğru, canlı API 200 döndü (P1-8)
- [ ] `CM_CHANNEL_SHORTCODE`'un kurumdan yazılı teyidi (P1-8)
- [ ] Test T.C. kimlik numarasının kurumdan temini (P1-8)

**İlk veri yükleme — sırayla, atlanmadan (P1-18):**

- [ ] `scripts.importer` → QnA verisi PostgreSQL + MeiliSearch'e (**yalnızca bir kez** — bkz. P1-19)
- [ ] `scripts.vector_sync` → Qdrant vektörleri
- [ ] `scripts.create_admin` → ilk super_admin (panele girebilmek için)
- [ ] Panelden akademik takvim CSV'sinin içe aktarılması
- [ ] **Ayarlar → LLM'in açılması** (varsayılan kapalı — bkz. P1-21)
- [ ] Bota "güz dönemi final sınavları ne zaman" sorulup **somut tarih** döndüğünün teyidi
- [ ] Bota "kayıt yenileme nasıl yapılır" sorulup **tarih değil prosedür** döndüğünün teyidi (P1-21)

**Test sırasında doğrulanacak:**

- [x] ~~`/api/solution-center/categories` ile SMS göndermeden auth doğrulaması~~ — **tamamlandı** (P1-8)
- [ ] Uçtan uca talep akışı: TC → SMS → OTP → öğrenci → kategori → talep → UUID (P1-8)
- [ ] Kategori seçim adımının tamamlanma oranı (4 tıklamaya kadar derinlik — P1-8b)
- [ ] Bakım modunun üç yolunun da çalıştığı (panel / otomatik 502 / `bakim.sh`)
- [ ] Yönetim uçlarının kilitli olduğu (`/api/qna` → 401)
- [ ] Backend portuna dışarıdan erişilemediği

---

## 6. Sonuç

Sistem, "cevap veren bir chatbot"un çok ötesinde; bilgi bankası yönetimi, semantik
arama, kullanım analitiği, rol tabanlı yetkilendirme, bakım/deploy altyapısı ve talep
yönetimini tek çatı altında birleştiriyor. Mimari kararlar tutarlı ve kod yorumlarında
gerekçelendirilmiş — bu, devralınabilirlik açısından projenin en değerli özelliği.

Tespit edilen eksiklerin çoğu **kod kalitesi sorunu değil, canlı ortam sertleştirmesi
(production hardening) boşluğu**. Bunlar bir sistemin geliştirme aşamasından test
aşamasına geçerken tipik olarak ortaya çıkan maddelerdir; erken yakalanmaları hâlinde
kapatılmaları da görece hızlıdır.

En acil olanı **P0-1 ve P0-2**: bu ikisi birlikte, sisteme dışarıdan SMS gönderten ve
başkası adına resmî talep açtırabilecek bir yüzey oluşturuyor. Test aşamasına
geçilmeden önce kapatılmaları önerilir.

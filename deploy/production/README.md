# Native production APP runtime

Bu dizin APP-01 ve APP-02 için aynı native Linux runtime ve deployment
sözleşmesini tanımlar. APP VM bootstrap, systemd runtime, harici environment
şablonu, Nginx application listener, production release artifact builder,
prepare/shared-init/activate lifecycle'ı, rollback ve temel operasyon komutları
bu kapsamdadır. Fiziksel LB, TLS, firewall ve PostgreSQL/MeiliSearch/Qdrant
sunucu kurulumu bu araçlar tarafından otomatikleştirilmez.

## Dosya sistemi sözleşmesi

```text
/opt/auzef/
├── releases/                         # versioned, immutable releases
├── current -> releases/<release-id>   # active release; installer oluşturmaz
└── previous -> releases/<release-id>  # son başarılı activation öncesi release

/etc/auzef/
└── backend.env                      # runtime config/secrets, root:auzef 0640

/var/cache/auzef/
└── huggingface/                     # HF_HOME; release'lerden bağımsız

/var/lib/auzef/
├── flags/
│   └── maintenance.flag              # yalnız APP-local acil override
└── locks/
    └── operation.lock                # root-owned operasyon kilidi
```

`releases/` deployment tarafından yönetilir ve yayınlandıktan sonra
değiştirilmez. `current` aktif release'i, `previous` son başarılı activation
öncesindeki release'i atomik symlink'lerle gösterir. Config, secret, model cache
ve local state release içine yazılmaz.

Planlı maintenance state'i local flag değildir: DB-ADMIN `SystemConfig`
içindeki `MAINTENANCE_MODE` kaydıdır ve APP-01/APP-02 tarafından ortak görülür.
Local flag sadece bulunduğu node'un Nginx `/widget-chat` akışını kapatan acil
override'dır.

## Bootstrap

Desteklenen hostta `systemctl`, `journalctl`, `nginx`, `python3.11`, `curl`,
`tar`, `sha256sum` ve standart Linux user/group/file araçları önceden kurulu
olmalıdır. Installer eksik paket kurmaz:

```sh
sudo ./deploy/production/app/install.sh
```

Script idempotent olarak `auzef` system user/group'unu ve gerekli dizinleri
oluşturur, izinleri uygular, backend systemd unit ile Nginx şablonunu ilk
kurulumda yerleştirir, `daemon-reload` ve `nginx -t` çalıştırır. Ayrıca
`auzef-init@.service` ve aşağıdaki operasyon araçlarını kurar:

```text
/usr/local/sbin/auzef-deploy
/usr/local/sbin/auzef-init
/usr/local/sbin/auzef-rollback
/usr/local/sbin/auzef-status
/usr/local/sbin/auzef-health
/usr/local/sbin/auzef-logs
/usr/local/sbin/auzef-common.sh
```

Operasyon scriptleri ve shared-init unit secret içermeyen, repo tarafından
yönetilen dosyalardır; installer tekrar çalıştırıldığında güncellenir.
Mevcut `/etc/auzef/backend.env`, backend systemd unit ve Nginx production
config'i ezilmez.

Installer bilerek şunları yapmaz:

- release veya `.venv` oluşturmaz;
- `current` symlink'ini oluşturmaz/değiştirmez;
- infrastructure initialization çalıştırmaz;
- backend veya Nginx servisini başlatmaz/enable etmez.

Gerçek config, `/etc/auzef/backend.env.example` temel alınarak operatör
tarafından oluşturulmalıdır. Production'da `ADMIN_DATABASE_URL` ve
`CHAT_DATABASE_URL` ayrı ayrı zorunludur; development `DATABASE_URL` fallback'ine
güvenilmez.

## systemd runtime

`auzef-backend.service` development `entrypoint.sh` dosyasını kullanmaz ve
schema/Qdrant provisioning yapmaz. Aktif release içinden doğrudan şunu çalıştırır:

```text
/opt/auzef/current/.venv/bin/uvicorn main:app
  --host 127.0.0.1 --port 8000 --workers 2
  --proxy-headers --forwarded-allow-ips=127.0.0.1
```

Working directory `/opt/auzef/current/backend`, runtime user/group `auzef` ve
environment file `/etc/auzef/backend.env`'dir. FastAPI yalnız loopback'te
dinler; LB backend portuna doğrudan erişmez. Restart politikası
`on-failure`, graceful stop süresi 45 saniyedir. stdout/stderr journald'a gider:

```sh
journalctl -u auzef-backend.service
```

Development Docker wrapper'ı ise kolaylık için
`python -m scripts.init_system all` komutunu Uvicorn'dan önce çalıştırmaya
devam eder.
Production deploy süreci explicit initialization komutlarını ayrı bir gate
olarak çağırmalıdır; installer ve backend runtime unit'i bunu üstlenmez.

## Nginx

Nginx Angular SPA'yı `/opt/auzef/current/frontend` altından sunar; API,
widget ve health trafiğini loopback Uvicorn'a proxy eder. Rate limit, 10 MiB
body limiti, asset cache, `index.html`/`widget.js` no-cache ve backend
502/503/504 maintenance fallback davranışları korunur. Listener/TLS ve gerçek
istemci IP sözleşmesi için [Nginx notlarına](app/nginx/README.md) bakın.

## Search service version contract

Native production server pinleri secret içermeyen
[`search-services.env`](search-services.env) dosyasındadır:

| Service | Application client | Selected native server |
|---|---|---|
| Meilisearch | `meilisearch==0.43.0` | `1.53.2` |
| Qdrant | `qdrant-client==1.19.0` | `1.19.1` |

Bu sürümler 2026-09-16 tarihinde
[`validation/search-services`](validation/search-services/README.md) altındaki
izole gerçek integration harness'ıyla doğrulandı. Meilisearch testi production
modunda master key zorunluluğunu ve keysiz index erişiminin reddedildiğini;
health/version, index CRUD, asynchronous task completion,
`showRankingScore`, `matchingStrategy=last` ve application searchable-attribute
sözleşmesini geçti. Qdrant testi API key açık server'da exact reported version,
`get_collections`, provider `ensure_collection`, cosine/model-dimension
collection create, canonical+alias payload upsert, `query_points`, provider
search, delete ve verified cleanup adımlarını geçti.

Meilisearch için warning oluşmadı. Qdrant client/server compatibility warning'i
oluşmadı; validation HTTP network'ünde test API key kullanıldığı için SDK
yalnız `Api key is used with an insecure connection.` uyarısını verdi. Bu
test-only transport production örneği değildir.

### Search security gate

- Meilisearch native service `MEILI_ENV=production`, güçlü master/API key ve
  yalnız yetkili APP/operations kaynaklarına açık private network ile
  çalışmalıdır. Meilisearch'in
  [configuration reference](https://www.meilisearch.com/docs/resources/self_hosting/configuration/reference)
  production modunda en az 16-byte master key'i zorunlu tutar.
- Qdrant'ın
  [official production checklist](https://qdrant.tech/documentation/production-checklist/)
  self-hosted instance'ların varsayılanda authentication olmadan tüm
  interface'lere açık olabildiğini ve API key'in minimum production adımı
  olduğunu belirtir. Native service API key, private-interface/network
  restriction ve TLS veya kurumun eşdeğer güvenli transport'u olmadan
  production trafiğine açılmamalıdır.
- Mevcut `QdrantProvider` ve `scripts.vector_sync` client oluştururken yalnız
  host/port geçirir; `QDRANT_API_KEY` wiring'i henüz yoktur. Server sürüm pini
  geçerlidir, fakat güvenli native rollout bu ayrı application/config
  değişikliği tamamlanmadan yapılamaz.

### Persistence, upgrade and rebuild assumptions

QnA, alias ve tag source-of-truth'u DB-ADMIN PostgreSQL'dir. Meilisearch ve
Qdrant yeniden üretilebilir derived search index'leridir:

- Meilisearch doldurma çekirdeği `backend/scripts/importer.py` içindeki
  `sync_meilisearch(db)` fonksiyonudur; aktif `qna_search_view` satırlarını
  `auzef_qna_index` içine yazar ve searchable attributes'i ayarlar. Importer'ın
  ana CLI'si PostgreSQL'e tekrar CSV import ettiği için salt production rebuild
  komutu değildir. Helper stale doküman temizliği, task-success gate'i veya
  atomik index swap sağlamaz.
- Meilisearch server upgrade'i mevcut `data.ms` üzerinde kör binary replacement
  olarak yapılmaz. Resmî
  [update guide](https://www.meilisearch.com/docs/resources/migration/updating)
  ile uyumlu snapshot/dump veya temiz instance/index + DB-ADMIN'den rebuild
  planı gerekir.
- Qdrant vector source-of-truth'u DB-ADMIN QnA metinleri, alias'lar ve aynı
  SentenceTransformer modelidir. `python -m scripts.vector_sync` aktif
  `qna_search_view` verisini `auzef_qna_vectors` collection'ına cosine
  vektörlerle yeniden upsert eder. Mevcut script stale point temizlemez ve
  collection dimension/distance uyuşmazlığını düzeltmez; temiz rebuild/swap
  runbook'u sonraki operasyon işidir. `init_system qdrant` yalnız collection
  provisioning yapar, veri rebuild'i yapmaz.

Bu görev snapshot/dump otomasyonu veya native installer eklemez. Qdrant'ın
[official installation guidance](https://qdrant.tech/documentation/installation/)
binary executable yolunu development/testing seçeneği olarak konumlandırır;
kurumun zorunlu native-production kararında binary provenance/checksum,
POSIX/block storage, authentication/TLS, monitoring, backup/restore, upgrade ve
HA sorumluluğu tamamen kurumun operasyon katmanında tanımlanmalıdır.

## Deployment lifecycle

Production deploy birbirinden ayrı üç fazdır:

1. **PREPARE:** Artifact doğrulanır, aynı filesystem'deki staging dizinine
   açılır, checksum kontrol edilir, `.venv` ve exact dependency kurulumu
   tamamlanır, runtime smoke gate geçilir ve immutable release yayınlanır.
   Aktif `current` değişmez.
2. **SHARED INIT:** DB-ADMIN, DB-CHAT ve Qdrant initialization yeni release
   koduyla operatör tarafından açıkça ve yalnız bir APP node'da çalıştırılır.
   Bu faz per-node activation'ın parçası değildir.
3. **ACTIVATE:** Drain edilmiş node'un `current` symlink'i atomik değiştirilir,
   yalnız backend restart edilir ve localhost Nginx üzerinden readiness gate
   beklenir. Başarısızlık application release rollback'ini tetikler.

Tüm operasyon komutları release dizini ve systemd state'i yönettiği için root
olarak (`sudo`) çalıştırılır. Deploy hiçbir yolda shared initialization'ı
otomatik çalıştırmaz.

### PREPARE

```sh
sudo auzef-deploy /path/to/auzef-4.1.0.tar.gz --prepare-only
```

Archive doğrudan `releases/` altına açılmaz. Önce absolute path, `..` path
traversal, birden fazla top-level dizin ve normal file/directory dışındaki
symlink, hardlink, device veya FIFO entry'leri reddedilir. Tek top-level dizin
`auzef-<VERSION>` olmalı; `VERSION`, `BUILD_INFO`, `SHA256SUMS`,
`backend/main.py`, `backend/requirements.lock`, `frontend/index.html` ve
`frontend/widget.js` bulunmalıdır. `sha256sum -c SHA256SUMS` geçmeden devam
edilmez. Aynı version daha önce hazırlanmışsa mevcut release overwrite veya
mutate edilmez.

Checksum corruption/integrity kontrolüdür; artifact authenticity veya imza
garantisi değildir. Artifact APP node'lara yalnız kurumun güvenilir transfer
kanalıyla taşınmalıdır.

Prepare, target release içinde `python3.11 -m venv .venv` çalıştırır ve
committed CPU lock'u tam olarak şu sözleşmeyle kurar:

```sh
<release>/.venv/bin/python -m pip install --no-deps \
  -r <release>/backend/requirements.lock
<release>/.venv/bin/python -m pip check
```

Pip upgrade edilmez ve production'da dependency version resolution yapılmaz.
Ardından `torch` ve `sentence_transformers` import edilir;
`torch.cuda.is_available() is False` ve `torch.version.cuda is None` zorunludur.
Native numerical runtime eksikse veya paket kaynağına erişilemiyorsa prepare
non-zero biter; activation ve `current` değişikliği olmaz. Installer/deploy OS
paketi kurmaz. Hazırlanan payload ve `.venv` `root:auzef` sahipliğinde runtime
tarafından okunabilir/çalıştırılabilir fakat yazılamaz durumda yayınlanır.

### Production environment preflight

Activation ve shared init öncesinde `/etc/auzef/backend.env` bulunmalı ve
secret değerlerini ekrana basmadan doğrulanmalıdır. `ADMIN_DATABASE_URL`,
`CHAT_DATABASE_URL`, `MEILI_URL`, `QDRANT_HOST` ve `QDRANT_PORT` boş olamaz;
`ADMIN_AUTH_ENFORCED=true`, `ADMIN_COOKIE_SECURE=true` ve
`HF_HOME=/var/cache/auzef/huggingface` zorunludur. `CHANGE_ME` placeholder'ı
kalamaz. Production, development `DATABASE_URL` fallback'ine güvenmez.
LLM/Solution Center alanları uygulamanın mevcut optional/conditional davranışına
göre boş olabilir; preflight bunları keyfi olarak zorunlu yapmaz.

### SHARED INIT

```sh
sudo auzef-init 4.1.0 --confirm-shared-change
```

Komut prepared release'i ve production environment'ı doğrular, ardından
`auzef-init@4.1.0.service` oneshot unit'iyle release'in `.venv` ortamında
`python -m scripts.init_system all` çalıştırır. Hata systemd ve komut exit
code'una yansır; başarı gibi maskelenmez. Loglar:

```sh
sudo auzef-logs --init 4.1.0
```

DB ve Qdrant APP-01/APP-02 arasında ortak olduğu için bu komut release başına
yalnız **bir node'da, bir kez** çalıştırılır. Shared schema/index change
diğer aktif APP'i etkileyebilir; gerektiğinde merkezi maintenance/change window
kullanılır.

### ACTIVATE ve automatic rollback

Node fiziksel LB'den drain edildikten sonra:

```sh
sudo auzef-deploy --activate 4.1.0 --confirm-drained
```

`--confirm-drained` LB'yi kontrol etmez; operatörün drain işlemini tamamladığına
dair zorunlu ve bilinçli onaydır. Komut target release'i, production environment'ı
ve `.venv/bin/uvicorn` dosyasını doğrular; geçici symlink + atomic rename ile
`current`'i değiştirir ve `auzef-backend.service` servisini restart eder. Nginx
config değişmediği için normal application deploy'unda reload edilmez.

Gate, varsayılan 180 saniyelik pencere boyunca kısa timeout'larla
`http://127.0.0.1/health/ready` adresini Nginx üzerinden poll eder. HTTP 200
`ready` veya `degraded` başarılıdır; HTTP 503, connection failure ve timeout
başarısızdır. Başarılı activation sonrası eski `current` varsa `previous`
atomik olarak onu gösterir.

Yeni release pencere içinde hazır olmazsa eski `current` atomik geri yüklenir,
backend tekrar restart edilir ve eski release readiness'i yeniden beklenir. Eski
release toparlansa bile deploy non-zero döner ve yeni deployment'ın başarısız,
previous release'in restore edildiğini bildirir. Eski release de toparlanmazsa
`CRITICAL` hata verilir ve iki failure da gizlenmez. İlk deployment'ta geri
dönülecek release yoksa servis durdurulur, broken `current` bırakılmaz ve komut
non-zero biter. Failed yeni release diagnosis için silinmez.

### Manuel rollback

```sh
sudo auzef-rollback --confirm-drained
sudo auzef-rollback 4.0.0 --confirm-drained
```

İlk komut `previous`, ikinci komut belirtilen prepared release'e döner. Target
validation, atomik `current` switch, backend restart ve aynı readiness gate
uygulanır. Rollback target hazır olmazsa başlangıçtaki `current` release geri
yüklenerek recovery denenir. Başarılı rollback'te `previous` eski `current`'i
gösterir.

Application rollback DB/Qdrant initialization'ı, schema/DDL'yi veya dependency
kurulumunu geri almaz. Bu nedenle shared initialization ile gelen schema
değişiklikleri eski application release'iyle backward-compatible olmalıdır.
Reverse migration/Alembic bu lifecycle'ın parçası değildir.

### Operasyon yardımcıları

```sh
sudo auzef-status
sudo auzef-health
sudo auzef-logs
sudo auzef-logs -f
sudo auzef-logs --init 4.1.0
```

`auzef-status` secret okumadan current/previous release'leri, backend ve Nginx
service state'ini, local emergency flag'i ve prepared release'leri gösterir.
`auzef-health`, localhost Nginx üzerinden `/health/live` ve `/health/ready` HTTP
code/body bilgisini gösterir; readiness HTTP 200 `degraded` automation için de
başarılıdır. `auzef-logs` yalnız journald wrapper'ıdır; ayrı application log
dosyası oluşturulmaz.

## İki APP node rolling deployment runbook

LB drain/add API'si otomatikleştirilmemiştir. Kurum prosedürüyle her adımı
operatör tamamlar:

1. Artifact'ı trusted kurum kanalıyla APP-01 ve APP-02'ye kopyalayın.
2. APP-01'de `auzef-deploy <artifact> --prepare-only` çalıştırın.
3. APP-02'de aynı artifact için `auzef-deploy <artifact> --prepare-only`
   çalıştırın.
4. Yalnız APP-01'de `auzef-init <version> --confirm-shared-change` çalıştırın;
   gerekliyse shared change için merkezi maintenance/change window kullanın.
5. APP-01'i fiziksel LB'den drain edin.
6. APP-01'de `auzef-deploy --activate <version> --confirm-drained` çalıştırın;
   readiness başarılıysa node'u LB'ye geri ekleyin.
7. APP-02'yi fiziksel LB'den drain edin.
8. APP-02'de `auzef-deploy --activate <version> --confirm-drained` çalıştırın;
   shared init'i tekrar çalıştırmayın.
9. APP-02 readiness başarılıysa node'u LB'ye geri ekleyin.
10. `auzef-status` ile her iki node'un aynı `VERSION` gösterdiğini doğrulayın.

Rollback gerektiğinde önce ilgili node LB'den drain edilir. Application
rollback'in shared DB/Qdrant change'ini geri almadığı unutulmamalıdır.

## Backend dependency lock lifecycle

`backend/requirements.txt` insan tarafından yönetilen direct dependency
listesidir. `backend/requirements.lock` ise production kurulumu için tüm
transitive graph'ı exact `package==version` satırlarıyla sabitler. Production
dependency target'ı kesin olarak Python 3.11, Linux ve CPU-only APP
runtime'dır. Development makinelerinde veya container'larda GPU kullanılması
bu production sözleşmesini değiştirmez.

Mevcut CPU lock baseline'ı, full backend test suite'inin geçtiği Python
`3.11.16` Linux backend snapshot'ındaki sürümleri korur. Yalnız
`torch==2.14.0`, PyTorch'un resmi CPU wheel indeksindeki
`torch==2.14.0+cpu` distribution'ıyla değiştirilmiş; CUDA/NVIDIA runtime
paketleri ve GPU derleyici runtime'ı graph'tan çıkarılmıştır. Sonuç 89
exact pakettir. Lock içindeki resmi CPU index direktifi, production
kurulumunda yanlış GPU distribution'ının seçilmesini önler.

Lock bilinçli bir dependency update sırasında şu build/development aracıyla
yenilenir:

```sh
./deploy/production/build/refresh-backend-lock.sh
```

Refresh aracı build-only `python:3.11-slim` image içinde, GPU device vermeden
önce mevcut torch sürümünü PyTorch resmi CPU indeksinden kurar; sonra unpinned
direct listeden graph'ı yeniden çözer. Oluşan aday lock ikinci bir temiz
image'da production komutu olan `pip install --no-deps` ile yeniden kurulur;
`pip check`, `sentence_transformers`/`torch` import'u ve
`torch.cuda.is_available() is False` geçmeden mevcut lock değiştirilmez.

Development environment snapshot'ı GPU paketleri içerebilir ve production
baseline kaynağı değildir. Refresh bilinçli bir dependency update işlemidir:
sonucu otomatik olarak "validated" sayılmaz; diff incelenmeli, uyumluluk
riskleri değerlendirilmeli ve full backend suite geçmeden commit edilmemelidir.
Release builder lock'u asla yenilemez; yalnız committed lock'u tüketir ve
`cuda-*`, `nvidia-*` veya `triton` package görürse build'i reddeder.

## Release artifact build

Repo kökünden temiz bir Git worktree ile:

```sh
./deploy/production/build/build-release.sh 4.1.0
```

Çıktı `dist/releases/auzef-4.1.0.tar.gz` olur. `dist/` zaten Git tarafından
ignore edilir. Builder aynı version artifact'ını overwrite etmez. Normal build
dirty worktree'de durur; yalnız test/development için açık
`--allow-dirty` override'ı kullanılabilir ve bu durumda `BUILD_INFO`
`dirty=true` kaydeder.

Frontend, host Node sürümüne bağlı değildir. Builder mevcut Dockerfile'ın
Node 20 build stage'ini ve `package-lock.json` dosyasını kullanır; bu stage
`npm ci --legacy-peer-deps` ve `ng build --configuration=production`
çalıştırır. Yalnız `dist/chatbot-web` sonucu artifact'a alınır.

Archive açıldığında tek top-level dizin vardır:

```text
auzef-4.1.0/
├── backend/
│   ├── main.py
│   ├── admin/ core/ integrations/ routers/ scripts/ services/
│   ├── requirements.txt
│   └── requirements.lock
├── frontend/
│   ├── index.html
│   ├── widget.js
│   └── production Angular assets
├── VERSION
├── BUILD_INFO
└── SHA256SUMS
```

`BUILD_INFO` version, Git commit, ref, UTC timestamp ve dirty bilgisini içerir;
environment veya secret yazmaz. `SHA256SUMS`, kendisi dışındaki tüm release
dosyalarını kapsar. Extract sonrası doğrulama:

```sh
cd auzef-4.1.0
sha256sum -c SHA256SUMS
```

Artifact bir deploy değildir: APP VM'e kopyalama, release activation,
`current` symlink değişikliği, initialization ve service restart yapmaz.
Secret/config, testler, frontend kaynak ağacı, Docker tooling, cache, model
weight, `node_modules` ve `.venv` archive'a girmez.

## Production dependency installation contract

Python virtualenv build hostundan production VM'e portable kabul edilmediği
için `.venv` artifact'a konmaz. `auzef-deploy --prepare-only`, release
activation öncesinde target APP node'da şu exact kurulumu yapar:

```sh
python3.11 -m venv <release>/.venv
<release>/.venv/bin/python -m pip install --no-deps \
  -r <release>/backend/requirements.lock
```

Lock transitive graph'ın tamamını içerdiğinden production'da version resolution
yapılmaz. Lock'un `--extra-index-url https://download.pytorch.org/whl/cpu`
direktifi torch'un aynı sürümdeki resmi CPU distribution'ını seçer. Paketlerin
PyPI, internet veya bir kurum kaynağından nasıl
taşınacağı bu aşamada varsayılmamıştır. VM internet erişimi yoksa sonraki
adımda wheelhouse veya internal package mirror sözleşmesi eklenecektir;
mevcut artifact wheelhouse içermez.

SentenceTransformer embedding inference'ı APP VM'lerde CPU ile çalışır.
Embedding model cache'i `/var/cache/auzef/huggingface` altında release'lerden
bağımsız persistent state olarak kalır. Model weights artifact'a gömülmez.
Qdrant provider application import sırasında SentenceTransformer modelini
yüklediği için cache activation'ın kritik girdisidir:

- APP VM outbound internet erişimine sahipse ilk backend startup model cache'ini
  doldurabilir ve readiness penceresi normalden uzun sürebilir.
- Outbound internet yoksa model cache activation'dan önce ayrıca preload veya
  kurum kanalıyla copy edilmelidir.
- Cache hazır değilse backend startup/readiness başarısız olabilir. Health gate
  yeni release'i başarılı kabul etmez ve eski çalışan release varsa automatic
  rollback dener.

Model revision pinleme, model bundle ve offline cache paketleme ayrı altyapı
kararıdır; bu deployment artifact'ının parçası değildir.

## Açık altyapı soruları

- TLS fiziksel Load Balancer üzerinde mi terminate edilecek?
- Production VM'lerin internet çıkışı olacak mı?
- PostgreSQL kurulumu BİDB tarafından mı yapılacak, uygulama ekibi mi yapacak?
- Load Balancer node drain/add operasyon prosedürü nedir?
- Fiziksel LB'nin trusted IP/CIDR listesi, istemci-IP header'ı ve APP listener
  portu nedir?

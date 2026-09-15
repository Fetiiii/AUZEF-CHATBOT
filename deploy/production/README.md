# Native production APP runtime

Bu dizin APP-01 ve APP-02 için aynı native Linux runtime sözleşmesini
tanımlar. APP VM bootstrap, systemd runtime, harici environment şablonu, Nginx
application listener ve production release artifact builder bu kapsamdadır.
Deploy/rollback, rolling deployment ve PostgreSQL/MeiliSearch/Qdrant kurulumu
henüz bu kapsamda değildir.

## Dosya sistemi sözleşmesi

```text
/opt/auzef/
├── releases/                         # versioned, immutable releases
└── current -> releases/<release-id>   # active release; installer oluşturmaz

/etc/auzef/
└── backend.env                      # runtime config/secrets, root:auzef 0640

/var/cache/auzef/
└── huggingface/                     # HF_HOME; release'lerden bağımsız

/var/lib/auzef/
└── flags/
    └── maintenance.flag              # yalnız APP-local acil override
```

`releases/` deployment tarafından yönetilir ve yayınlandıktan sonra
değiştirilmez. `current` atomik release seçimini temsil eder. Config, secret,
model cache ve local state release içine yazılmaz.

Planlı maintenance state'i local flag değildir: DB-ADMIN `SystemConfig`
içindeki `MAINTENANCE_MODE` kaydıdır ve APP-01/APP-02 tarafından ortak görülür.
Local flag sadece bulunduğu node'un Nginx `/widget-chat` akışını kapatan acil
override'dır.

## Bootstrap

Desteklenen hostta `systemctl`, `nginx`, `python3.11` ve standart Linux
user/group araçları önceden kurulu olmalıdır. Installer eksik paket kurmaz:

```sh
sudo ./deploy/production/app/install.sh
```

Script idempotent olarak `auzef` system user/group'unu ve gerekli dizinleri
oluşturur, izinleri uygular, systemd unit ile Nginx şablonunu ilk kurulumda
yerleştirir, `daemon-reload` ve `nginx -t` çalıştırır. Mevcut
`/etc/auzef/backend.env`, systemd unit veya Nginx production config'i ezilmez.

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
olarak çağırmalıdır; bu installer veya systemd unit bunu üstlenmez.

## Nginx

Nginx Angular SPA'yı `/opt/auzef/current/frontend` altından sunar; API,
widget ve health trafiğini loopback Uvicorn'a proxy eder. Rate limit, 10 MiB
body limiti, asset cache, `index.html`/`widget.js` no-cache ve backend
502/503/504 maintenance fallback davranışları korunur. Listener/TLS ve gerçek
istemci IP sözleşmesi için [Nginx notlarına](app/nginx/README.md) bakın.

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
için `.venv` artifact'a konmaz. Sonraki deployment katmanı, release activation
öncesinde target APP node'da şu exact kurulumu yapacaktır:

```sh
python3.11 -m venv <release>/.venv
<release>/.venv/bin/pip install --no-deps -r <release>/backend/requirements.lock
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
Preload/offline stratejisi VM internet erişimi kesinleştiğinde ele alınacaktır.

## Açık altyapı soruları

- TLS fiziksel Load Balancer üzerinde mi terminate edilecek?
- Production VM'lerin internet çıkışı olacak mı?
- PostgreSQL kurulumu BİDB tarafından mı yapılacak, uygulama ekibi mi yapacak?
- Load Balancer node drain/add operasyon prosedürü nedir?
- Fiziksel LB'nin trusted IP/CIDR listesi, istemci-IP header'ı ve APP listener
  portu nedir?

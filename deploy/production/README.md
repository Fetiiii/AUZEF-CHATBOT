# Native production APP runtime

Bu dizin APP-01 ve APP-02 için aynı native Linux runtime sözleşmesini
tanımlar. Kapsam yalnız APP VM bootstrap, systemd runtime, harici environment
şablonu ve Nginx application listener'dır. Release artifact oluşturma,
deploy/rollback, rolling deployment ve PostgreSQL/MeiliSearch/Qdrant kurulumu bu
aşamanın dışındadır.

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

Development Docker wrapper'ı ise kolaylık için `python -m
scripts.init_system all` komutunu Uvicorn'dan önce çalıştırmaya devam eder.
Production deploy süreci explicit initialization komutlarını ayrı bir gate
olarak çağırmalıdır; bu installer veya systemd unit bunu üstlenmez.

## Nginx

Nginx Angular SPA'yı `/opt/auzef/current/frontend` altından sunar; API,
widget ve health trafiğini loopback Uvicorn'a proxy eder. Rate limit, 10 MiB
body limiti, asset cache, `index.html`/`widget.js` no-cache ve backend
502/503/504 maintenance fallback davranışları korunur. Listener/TLS ve gerçek
istemci IP sözleşmesi için [Nginx notlarına](app/nginx/README.md) bakın.

## Açık altyapı soruları

- TLS fiziksel Load Balancer üzerinde mi terminate edilecek?
- Production VM'lerin internet çıkışı olacak mı?
- PostgreSQL kurulumu BİDB tarafından mı yapılacak, uygulama ekibi mi yapacak?
- Load Balancer node drain/add operasyon prosedürü nedir?
- Fiziksel LB'nin trusted IP/CIDR listesi, istemci-IP header'ı ve APP listener
  portu nedir?

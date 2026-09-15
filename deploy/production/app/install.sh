#!/bin/sh
set -eu

APP_USER=auzef
APP_GROUP=auzef
APP_ROOT=/opt/auzef
CONFIG_DIR=/etc/auzef
BACKEND_ENV=/etc/auzef/backend.env
CACHE_DIR=/var/cache/auzef/huggingface
STATE_DIR=/var/lib/auzef
FLAGS_DIR=/var/lib/auzef/flags
SYSTEMD_TARGET=/etc/systemd/system/auzef-backend.service
NGINX_TARGET=/etc/nginx/conf.d/auzef-app.conf

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UNIT_SOURCE=$SCRIPT_DIR/auzef-backend.service
NGINX_SOURCE=$SCRIPT_DIR/nginx/auzef-app.conf
ENV_SOURCE=$SCRIPT_DIR/../config/backend.env.example
ENV_EXAMPLE_TARGET=$CONFIG_DIR/backend.env.example

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    fail "Bu bootstrap scripti root veya sudo ile calistirilmalidir."
fi

missing=""
for command_name in systemctl nginx python3.11 getent groupadd useradd install; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        missing="$missing $command_name"
    fi
done
if [ -n "$missing" ]; then
    fail "Eksik prerequisite binary'ler:$missing. Paketleri kurum proseduruyle kurup tekrar deneyin; installer paket kurmaz."
fi

[ -f "$UNIT_SOURCE" ] || fail "Systemd unit template bulunamadi: $UNIT_SOURCE"
[ -f "$NGINX_SOURCE" ] || fail "Nginx template bulunamadi: $NGINX_SOURCE"
[ -f "$ENV_SOURCE" ] || fail "Environment template bulunamadi: $ENV_SOURCE"
[ -d /etc/nginx/conf.d ] || fail "/etc/nginx/conf.d bulunamadi; Nginx paket yapisini kontrol edin."
[ -x /usr/sbin/nologin ] || fail "/usr/sbin/nologin bulunamadi; service account shell gereksinimini kontrol edin."

if ! getent group "$APP_GROUP" >/dev/null 2>&1; then
    groupadd --system "$APP_GROUP"
    printf 'System group olusturuldu: %s\n' "$APP_GROUP"
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --gid "$APP_GROUP" --home-dir "$APP_ROOT" \
        --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    printf 'System user olusturuldu: %s\n' "$APP_USER"
fi

# Releases and current are deployment-owned. Runtime may read them but this
# bootstrap intentionally does not create or change the current symlink.
install -d -o root -g "$APP_GROUP" -m 0755 "$APP_ROOT" "$APP_ROOT/releases"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$CACHE_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0755 "$STATE_DIR" "$FLAGS_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$CONFIG_DIR"

# Refreshing the public template is safe; the real secret file is never
# generated or overwritten by this installer.
install -o root -g root -m 0644 "$ENV_SOURCE" "$ENV_EXAMPLE_TARGET"
if [ -e "$BACKEND_ENV" ]; then
    chown root:"$APP_GROUP" "$BACKEND_ENV"
    chmod 0640 "$BACKEND_ENV"
    printf 'Mevcut production secret/config korundu: %s\n' "$BACKEND_ENV"
else
    printf 'Production config eksik. %s sablonundan %s olusturun; secret degerlerini elle girin.\n' \
        "$ENV_EXAMPLE_TARGET" "$BACKEND_ENV"
fi

install_if_absent() {
    source_path=$1
    target_path=$2
    mode=$3
    if [ -e "$target_path" ]; then
        printf 'Mevcut production dosyasi korundu: %s\n' "$target_path"
    else
        install -o root -g root -m "$mode" "$source_path" "$target_path"
        printf 'Kuruldu: %s\n' "$target_path"
    fi
}

install_if_absent "$UNIT_SOURCE" "$SYSTEMD_TARGET" 0644
install_if_absent "$NGINX_SOURCE" "$NGINX_TARGET" 0644

systemctl daemon-reload
nginx -t

printf '%s\n' 'APP node bootstrap tamamlandi.'
printf '%s\n' 'Release deploy edilmedi, /opt/auzef/current olusturulmadi ve servis baslatilmadi.'
printf '%s\n' 'Backend config, release ve trusted LB CIDR hazirlanmadan servisi enable/start etmeyin.'

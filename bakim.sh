#!/bin/sh
# AUZEF Chatbot — APP-local/acil bakım override aracı.
#
# İki kullanım şekli:
#   ./bakim.sh                → etkileşimli menü: sistem durumu özeti +
#                                     numaralı seçimler. Projeyi hiç bilmeyen
#                                     biri için: numarayı yaz, Enter'a bas.
#   ./bakim.sh on|off|status  → tek komut (otomasyon / deneyimli kullanıcı)
#
# Panel/backend ÇALIŞMIYORKEN bile çalışır: yalnız bu makinedeki nginx
# (frontend) container'ının bakım bayrağını yönetir, backend'e hiç dokunmaz.
# Merkezi planlı bakımın normal yolu panelde Ayarlar → Bakım Modu'dur ve
# DB-ADMIN'e yazılır; bu script o merkezi state'i okumaz veya değiştirmez.
#
# Bayrak varken bu APP'in nginx'i /widget-chat'e 503 döner (widget "bakımdayız"
# der); admin paneli ve /api/ etkilenmez. Bayrak ops_flags volume'unda yaşar.
# İki APP'li production'da bu override diğer node'u ETKİLEMEZ; merkezi bakım
# amacıyla kullanılmamalıdır. Merkezi production CLI daha sonra eklenecektir.
#
# NOT: Container yolu docker exec'e hep `sh -c "..."` içinde gömülü verilir,
# asla doğrudan argüman olarak değil — Windows Git Bash'te MSYS, /etc/... ile
# başlayan argümanları C:/Program Files/Git/etc/... yoluna çevirir (yaşandı).

FLAG=/etc/nginx/flags/maintenance.flag
CONTAINER=auzef_frontend

# ── Yardımcılar ──────────────────────────────────────────────────────────────

container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

container_line() {  # $1=container adı  $2=etiket
  state=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' "$1" 2>/dev/null)
  case "$state" in
    running*) durum="ÇALIŞIYOR${state#running}" ;;
    "")       durum="YOK — container bulunamadı" ;;
    *)        durum="DURMUŞ ($state)" ;;
  esac
  printf "    %-12s: %s\n" "$2" "$durum"
}

# ── Temel işlemler ───────────────────────────────────────────────────────────

flag_on() {
  docker exec "$CONTAINER" sh -c "echo \"bakim.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)\" > $FLAG" \
    && echo "APP-local/acil override AÇIK — yalnız bu node widget'ı 503 döndürür."
}

flag_off() {
  docker exec "$CONTAINER" sh -c "rm -f $FLAG" \
    && echo "APP-local/acil override KAPALI — bu node backend state'ini izler."
}

flag_status() {
  if docker exec "$CONTAINER" sh -c "test -f $FLAG" 2>/dev/null; then
    printf "AÇIK — açan: "
    docker exec "$CONTAINER" sh -c "cat $FLAG"
  elif container_running "$CONTAINER"; then
    echo "KAPALI"
  else
    echo "bilinmiyor (frontend container'ı çalışmıyor)"
  fi
}

show_status() {
  echo
  echo "  Sistem durumu:"
  container_line auzef_backend  backend
  container_line auzef_frontend frontend
  container_line auzef_db       db
  container_line auzef_meili    meilisearch
  container_line auzef_qdrant   qdrant
  if docker exec auzef_backend sh -c "wget -qO- http://localhost:8000/health" >/dev/null 2>&1; then
    printf "    %-12s: OK\n" "/health"
  else
    printf "    %-12s: ULAŞILAMIYOR\n" "/health"
  fi
  # printf %-12s bayt sayar, "ı" 2 bayttır → Türkçe etikette elle boşluk
  printf "    Local override: "
  flag_status
  echo
}

usage() {
  echo "Kullanım: $0 [on|off|status]"
  echo "  on      Bu APP'te acil local override'ı aç"
  echo "  off     Bu APP'te acil local override'ı kapat"
  echo "  status  Bu APP'in local override durumunu göster"
  echo "  NOT: Merkezi planlı bakım DB-ADMIN üzerinden panelden yönetilir."
  echo "  Argümansız çalıştırırsanız durum özeti + etkileşimli menü açılır."
}

# ── Etkileşimli menü ─────────────────────────────────────────────────────────

menu() {
  echo
  echo "  AUZEF Chatbot — APP-local/Acil Override"
  echo "  ───────────────────────────"
  while :; do
    show_status
    echo "  Ne yapmak istersiniz?"
    echo "    1) Local override'ı AÇ    (yalnız bu node)"
    echo "    2) Local override'ı KAPAT (yalnız bu node)"
    echo "    3) Durumu yenile"
    echo "    0) Çık"
    printf "  Seçim: "
    read -r secim || exit 0
    case "$secim" in
      1)
        printf "  Yalnız bu APP node'u widget'a kapanacak. Emin misiniz? (e/h): "
        read -r onay || exit 0
        if [ "$onay" = "e" ] || [ "$onay" = "E" ]; then
          echo; flag_on
        else
          echo "  İptal edildi."
        fi
        ;;
      2) echo; flag_off ;;
      3) ;;  # döngü başı durumu zaten yeniden basar
      0|q|Q) exit 0 ;;
      *) echo "  Geçersiz seçim (0, 1, 2 veya 3 yazın)." ;;
    esac
  done
}

# ── Giriş noktası ────────────────────────────────────────────────────────────

case "$1" in
  on)     flag_on ;;
  off)    flag_off ;;
  status) printf "APP-local override: "; flag_status ;;
  "")     menu ;;
  *)      usage; exit 1 ;;
esac

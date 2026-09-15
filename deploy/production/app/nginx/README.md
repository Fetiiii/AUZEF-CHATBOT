# Native Nginx APP listener

`auzef-app.conf`, fiziksel Load Balancer arkasındaki APP node için HTTP
application listener'ıdır. Varsayılan port `80`'dir. Port, BİDB'nin Load
Balancer pool tanımıyla birlikte kararlaştırılmalı ve gerekirse iki tarafta da
değiştirilmelidir.

Nginx statik Angular dosyalarını `/opt/auzef/current/frontend` altından sunar
ve backend isteklerini yalnız `127.0.0.1:8000` adresine iletir. Uvicorn internete
veya fiziksel Load Balancer'a doğrudan açılmaz.

## TLS

Bu konfigürasyon sertifika, HTTPS listener veya HTTP-to-HTTPS redirect içermez.
TLS'in fiziksel Load Balancer'da mı APP Nginx'te mi sonlandırılacağı henüz
kesin değildir. APP node TLS termination gerekirse sertifika yolları ve TLS
wrapper/config, kurumun sağlayacağı bilgilerle ayrıca eklenmelidir.

## Gerçek istemci IP'si

Rate limit ve loglar `$remote_addr` kullanır. Kurumdan fiziksel Load Balancer'ın
kesin IP/CIDR listesi ve kullandığı istemci-IP header'ı alınmalıdır. Her
güvenilen ağ için konfigürasyondaki yorumun yanına şu direktif eklenir:

```nginx
set_real_ip_from <BIDB_TRUSTED_LB_CIDR>;
```

Mevcut tasarım `real_ip_header X-Forwarded-For` ve `real_ip_recursive on`
kullanır. `set_real_ip_from` tanımlanmadan gelen header'a güvenilmez; bu güvenli
varsayılanda log ve rate limit anahtarı istemci yerine LB IP'si olur. `0.0.0.0/0`
gibi tüm interneti güvenilir proxy yapan bir ağ kesinlikle eklenmemelidir. LB
farklı bir header veya PROXY protocol kullanıyorsa tasarım BİDB bilgisiyle
güncellenmelidir.

Nginx, doğruladığı `$remote_addr` değerini `X-Real-IP` ve
`X-Forwarded-For` olarak Uvicorn'a yeniden yazar. Dışarıdan gelen zincir
doğrudan backend'e aktarılmaz. TLS LB'de sonlandırılırsa public scheme bilgisinin
hangi güvenilir header ile taşınacağı da aynı altyapı kararıyla belirlenmelidir.

## Maintenance davranışı

Merkezi planlı maintenance state'i DB-ADMIN `SystemConfig` içindedir ve backend
`/widget-chat` için `503` üretir. Nginx bunu mevcut `@maintenance` JSON cevabına
çevirir. `/var/lib/auzef/flags/maintenance.flag` ise yalnız bu APP node'una
etki eden acil/local override'dır; APP-01'de oluşturulması APP-02'yi kapatmaz.

Kurulumdan sonra ve her manuel değişiklikte:

```sh
nginx -t
systemctl reload nginx
```

Gerçek production konfigürasyonu installer tarafından tekrar çalıştırmada
ezilmez. Repo şablonuyla farklar operatör tarafından incelenmelidir.

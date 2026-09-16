# Search service compatibility validation

Bu araç production'da container kullanılacağı anlamına gelmez. Yalnız aday
native server sürümleri seçilmeden önce development/test ortamında gerçek
client/server protokolünü doğrulamak için izole Docker container'ları kullanır.

```sh
./deploy/production/validation/search-services/run.sh
```

Harness benzersiz bir Compose project adı kullanır, host portu yayınlamaz,
developer Compose servislerine/volume'larına bağlanmaz ve bitince kendi
container, network ve volume'larını temizler. Test index/collection adları her
çalıştırmada benzersizdir ve validator ayrıca `finally` içinde siler.

Validator image Python 3.11 kullanır. `qdrant-client==1.19.0` ve
`meilisearch==0.43.0` kurulumunun transitive seçimleri committed
`backend/requirements.lock` tarafından constraint edilir. Gerçek HuggingFace
modeli indirilmez; Qdrant provider'ın mevcut API ve payload davranışı,
deterministik küçük vektörler üreten fake modelle gerçek server'a karşı
çalıştırılır.

Meilisearch master key, Qdrant ise yalnız testte kullanılan sabit bir API key
ile başlar. Bunlar production credential'ı değildir. Qdrant validator mevcut
provider'ın operasyon metotlarına authenticated SDK client enjekte eder;
mevcut application constructor'ı henüz `QDRANT_API_KEY` taşımadığı için güvenli
native rollout öncesinde ayrı bir application/config değişikliği gerekir.
Production'da API key, TLS/güvenli transport ve private network restriction
uygulanmadan Qdrant açılmamalıdır.

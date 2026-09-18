# AUZEF Answer Pipeline V2 Architecture

**Belge:** `docs/ANSWER_PIPELINE_V2_ARCHITECTURE.md`  
**Tarih:** 2026-09-18  
**Durum:** Mimari karar tabanı — implementasyon planlaması için hazır  
**Kapsam:** AUZEF Chatbot cevaplama hattı  
**Mevcut kod referansı:** `production-readiness` branch, audit edilen commit `e298535`  
**Not:** Bu belge implementasyon detayı veya görev planı değildir. Önce davranış sözleşmesini ve mimari sınırları sabitler; kod değişiklikleri ayrı görevlerde yapılacaktır.

---

## 1. Amaç

Bu belgenin amacı AUZEF Chatbot'un cevap üretme hattını model bağımlılığını azaltacak, hata davranışını öngörülebilir hale getirecek ve model/provider değişikliklerine daha dayanıklı olacak şekilde yeniden tanımlamaktır.

V2'nin hedefi LLM'i sistemden çıkarmak değildir. Hedef:

> **Deterministik olarak çözülebilen kararları kod ve veri politikalarıyla çözmek; gerçekten semantik yorum gerektiren dar belirsizlik alanlarında LLM kullanmak.**

LLM sistemin tamamının davranışını belirleyen merkez olmamalıdır. LLM, sınırları önceden belirlenmiş görevlerde çalışan bir yetenek bileşeni olmalıdır.

Bu belge aşağıdaki sorulara cevap verir:

- Kullanıcı mesajı nasıl anlaşılacak?
- Tek ve çoklu niyet nasıl ayrılacak?
- Conversation context nasıl kullanılacak?
- Academic Calendar ne zaman devreye girecek?
- MeiliSearch ve Qdrant'ın rolü nedir?
- Candidate eligibility ne anlama gelir?
- Selector neye karar verebilir?
- `NONE`, model hatası ve LLM-off modu nasıl ayrılacak?
- Model/provider konfigürasyonu nasıl yönetilecek?
- Model davranışı production'da nasıl gözlemlenecek?
- Hangi konular özellikle sonraki deneylere bırakılmıştır?

---

## 2. Neden V2?

Mevcut pipeline'ın güçlü bir temeli vardır:

- LLM serbest cevap üretmez; curated QnA cevabını seçer.
- MeiliSearch ve Qdrant birbirini tamamlayan retrieval kanallarıdır.
- QnA cevapları verbatim döner.
- Routing guard deterministik ve fail-closed çalışır.
- Candidate sırası reproducible olacak şekilde stabilize edilmiştir.
- Final multi-intent composition ek bir generative çağrı kullanmaz.
- Provider adapter sınırları büyük ölçüde temizdir.

Sorun retrieval'ın veya curated-answer yaklaşımının başarısız olması değildir.

Ölçüm ve hata analizi şu tabloyu göstermiştir:

- Gold QnA candidate pool'a yaklaşık **%99,6** oranında ulaşmaktadır.
- Recall@3 yaklaşık **%98,2**, Recall@10 yaklaşık **%99,6** seviyesindedir.
- Routing guard benchmarkında korumalı gold içeriklerin tamamı selector'a ulaşmış, fallback leakage gözlenmemiştir.
- Ana hata kümesi retrieval sonrasındaki **intent splitting ve candidate selection** kararlarındadır.
- 4o-mini tek-niyetli mesajları gereksiz bölmeye yatkındır.
- Luna-high aynı görevde farklı bir hata profili göstermiştir; doğru aday ilk sıradayken aşağıdaki adayı seçebildiği çok sayıda vaka vardır.
- Academic Calendar kayıtlarının her LLM selector havuzuna eklenmesi ölçülmüş yanlış cevaplara yol açmıştır.
- KB'deki bazı yakın QnA kümeleri iki model için de ayrım problemi oluşturmaktadır.
- Model, reasoning seviyesi veya provider değiştiğinde chatbot davranışı belirgin biçimde değişebilmektedir.

İnsan review sonrası mevcut değerlendirme tabanı:

- 503 evaluation target
- Bunların 17'si gerçek context-required ancak henüz puanlanabilir expected QnA'sı yok
- 486 puanlanabilir target
- 4o-mini exact: 362/486 ≈ **%74,5**
- Luna-high exact: 364/486 ≈ **%74,9**
- Exact paired karşılaştırmada anlamlı bir model üstünlüğü görülmemiştir
- Relaxed metrik, fazladan cevap üretimini ödüllendirebildiği için ana kalite metriği olarak kullanılmamalıdır

Bu nedenle V2'nin ana amacı yeni bir retrieval sistemi yazmak değil; **karar hattını sadeleştirmek ve LLM'nin yetki alanını doğru yerde sınırlamaktır.**

---

## 3. Tasarım İlkeleri

### 3.1 Progressive narrowing

Pipeline ilerledikçe karar alanı daralmalıdır.

LLM'e başta "ne yapacağına sen karar ver" denmemelidir.

Tercih edilen akış:

1. Kullanıcı mesajını anlamlandır.
2. Gerçekten birden fazla bağımsız intent var mı belirle.
3. Gerekli bilgi kaynaklarını aç.
4. Objektif olarak kullanılamaz adayları ele.
5. Kalan semantik belirsizliği LLM selector'a bırak.
6. Curated cevabı verbatim döndür.

### 3.2 Tek-niyet olağan, çoklu-niyet istisnadır

Production kullanımında mesajların ezici çoğunluğu tek niyetlidir. Mimari bunun tersini varsaymamalıdır.

### 3.3 LLM cevabı yazmaz

V2'de de LLM curated answer dışına serbest cevap üretmemelidir.

### 3.4 Current user turn primary truth'tur

Geçmiş konuşma yardımcı bağlamdır. Mevcut açık kullanıcı mesajının anlamını geçmiş konuşma değiştirmemelidir.

### 3.5 Eligibility selection değildir

Candidate eligibility yalnız objektif olarak kullanılamaz adayları eler.

"Hangisi daha iyi cevap?" sorusu selector'ın işidir.

### 3.6 Retrieval score semantik truth değildir

Qdrant/Meili score, rank veya provider bilgisi candidate oluşturma ve sıralamada kullanılabilir; selector'a semantik doğruluk kanıtı olarak zorunlu biçimde verilmez.

### 3.7 Semantic `NONE` finaldir

Selector adayları gördükten sonra `NONE` dediyse aynı Meili/Qdrant sonuçlarına dönüp başka bir threshold ile cevap zorlanmaz.

### 3.8 Model hatası ile `NONE` aynı değildir

`NONE`, `ERROR`, `INVALID_OUTPUT` ve sistemsel `LLM_OFF/UNAVAILABLE` ayrı durumlardır.

### 3.9 Model architecture değildir, configuration'dır

Intent Analyzer ve Selector ayrı capability'lerdir. Aynı modeli kullanabilirler fakat kullanmak zorunda değildirler.

### 3.10 Online LLM az iş yapar, offline analiz çok gözlemler

Runtime modelinden confidence veya kendi kararını açıklayan reason-code istemek yerine karar trace'i tutulur ve hata nedenleri offline analizde türetilir.

---

## 4. Hedef V2 Akışı

```text
                         USER TURN
                             │
                             ▼
                    validation / limits
                             │
                             ▼
                   bounded recent context
                   (previous 2 user turns)
                             │
                             ▼
                    ┌─────────────────┐
                    │ INTENT ANALYZER │
                    └────────┬────────┘
                             │
          1 veya en fazla 2 resolved intent
                             │
              her intent için calendar_relevant
                             │
                ┌────────────┴────────────┐
                │                         │
                ▼                         ▼
         QnA retrieval             Calendar retrieval
       Meili + Qdrant          yalnız relevant intent'te
                │                 filtreli event adayları
                └────────────┬────────────┘
                             ▼
                   CANDIDATE ELIGIBILITY
              active / guard / validity / source
                             │
                             ▼
                     bounded candidates
                             │
                             ▼
                    ┌─────────────────┐
                    │  LLM SELECTOR   │
                    └────────┬────────┘
                             │
                    SELECT(qna_id) / NONE
                             │
                ┌────────────┴────────────┐
                │                         │
             SELECT                      NONE
                │                         │
                ▼                         ▼
       curated answer verbatim     no QnA answer
                │
                └────────────┬────────────┘
                             ▼
                  deterministic composition
                             │
                             ▼
                        FINAL RESPONSE
```

LLM sistemsel olarak kapalı veya uzun süre unavailable ise ayrı **degraded mode** devreye girer. Bu durum semantic `NONE` ile karıştırılmaz.

---

## 5. Intent Analyzer V2

### 5.1 Amaç

Eski "splitter" yerine **Intent Analyzer** kullanılacaktır.

Intent Analyzer'ın görevi:

1. Mevcut kullanıcı turn'ünde kaç bağımsız cevap ihtiyacı olduğunu belirlemek.
2. En fazla iki intent çıkarmak.
3. Minimal dil normalizasyonu yapmak.
4. Gerekiyorsa önceki kullanıcı turnlerinden açık referansı çözmek.
5. Her intent için Calendar bilgisinin ilgili olup olmadığını işaretlemek.

Intent Analyzer:

- QnA seçmez.
- Calendar event seçmez.
- Cevap üretmez.
- Retrieval yapmaz.
- Kullanıcının söylemediği yeni semantik bilgi eklemez.

### 5.2 SINGLE / MULTI sözleşmesi

Olağan davranış:

`SINGLE`

`MULTI` yalnızca iki parçanın da **bağımsız olarak cevap gerektiren kullanıcı hedefleri** olması halinde kullanılmalıdır.

**Tek intent örneği**

> "Harcı yatırdım ama sisteme giremiyorum."

"Harcı yatırdım" ayrı bir kullanıcı talebi değildir; giriş probleminin bağlamıdır.

**Multi-intent örneği**

> "Sisteme giremiyorum, bir de ders materyallerine nereden ulaşacağım?"

İki taraf da diğerinden bağımsız olarak cevaplanabilir.

Ürün tanımı:

> **Intent = bağımsız cevap gereksinimi.**

Intent sayısı cümle, bağlaç veya soru işareti sayısı değildir.

### 5.3 Maksimum iki intent

V2 Intent Analyzer en fazla **2 intent** üretir.

Gerekçe:

- Gerçek kullanımın büyük çoğunluğu single-intent'tir.
- Gözlenen multi-intent mesajlar pratikte iki bağımsız talep seviyesindedir.
- Sınırsız split, maliyet ve yanlış cevap çoğalmasını büyütür.
- Üç veya daha fazla bağımsız kullanıcı talebi için ayrı bir UX/policy ileride tanımlanabilir.

Model 3+ intent üretmeye yetkili olmayacaktır.

### 5.4 Reformülasyon politikası

Intent Analyzer minimal normalizasyon yapabilir.

**İzinli**

> "derslere nerden bakcam"  
> → "Derslere nereden bakacağım?"

**İzinli context çözümleme**

Önceki user turn:

> "Harç ödemesini nasıl yapabilirim?"

Current:

> "Peki son günü ne zaman?"

Resolved:

> "Harç ödemesinin son günü ne zaman?"

**Yasak semantic expansion**

> "pasoyu nasıl alcam"  
> → "İstanbulkart öğrenci kartı başvurusunu e-Devlet üzerinden nasıl yapabilirim?"

Bu dönüşüm kullanıcıda olmayan bilgi eklediği için yasaktır.

### 5.5 Önerilen çıktı sözleşmesi

Kavramsal sözleşme:

```json
{
  "intent_count": 1,
  "intents": [
    {
      "source_text": "Peki son günü ne zaman?",
      "normalized_text": "Peki son günü ne zaman?",
      "resolved_text": "Harç ödemesinin son günü ne zaman?",
      "context_used": true,
      "calendar_relevant": true
    }
  ]
}
```

Multi örneği:

```json
{
  "intent_count": 2,
  "intents": [
    {
      "source_text": "sisteme giremiyorum",
      "normalized_text": "Sisteme giremiyorum.",
      "resolved_text": "Sisteme giremiyorum.",
      "context_used": false,
      "calendar_relevant": false
    },
    {
      "source_text": "bir de ders materyallerine nerden ulaşırım",
      "normalized_text": "Ders materyallerine nereden ulaşabilirim?",
      "resolved_text": "Ders materyallerine nereden ulaşabilirim?",
      "context_used": false,
      "calendar_relevant": false
    }
  ]
}
```

Kesin JSON schema implementasyon aşamasında seçilebilir ancak davranış sözleşmesi değişmemelidir.

### 5.6 Hata davranışı

Intent Analyzer model/parse hatasında:

- güvenli davranış `SINGLE`
- current user turn doğrudan intent olarak korunur
- Calendar relevance yanlışlıkla `true` yapılmaz
- hata observability trace'ine kaydedilir

Bu davranış request-level fail-safe'dir; tüm sistemi otomatik olarak `LLM_OFF` moduna geçirmek anlamına gelmez.

---

## 6. Context V2

### 6.1 İlk sürüm politikası

Intent Analyzer:

- current user turn
- önceki maksimum **2 user turn**

görür.

İlk sürümde bot cevapları context'e verilmez.

Gerekçe:

- Current turn ana gerçeklik olarak kalır.
- Bot cevapları gereksiz prompt gürültüsü veya önceki yanlış cevabın taşınması riskini artırabilir.
- User-only yaklaşımın yetersiz olduğu durumlar ayrı testle ölçülebilir.

### 6.2 Context kullanım kuralı

> **Context yalnız current user turn içindeki referans, eksilti veya gerçek follow-up anlamını çözmek için kullanılabilir.**

Current message kendi başına açıksa geçmiş konu taşınmamalıdır.

Örnek:

Önceki user:

> "Harç ödeme nasıl yapılır?"

Current:

> "İstanbulkart nasıl alabilirim?"

Resolved intent yine:

> "İstanbulkart nasıl alabilirim?"

olmalıdır.

### 6.3 Context pipeline boyunca dağılmamalıdır

V2'de mümkünse:

```text
recent user context
      ↓
Intent Analyzer
      ↓
resolved intent
      ↓
retrieval
      ↓
selector
```

Retrieval ve Selector'a tekrar full conversation history verilmesi varsayılan davranış olmayacaktır.

Bu yaklaşım mevcut sistemdeki context kullanım tutarsızlığını azaltmayı amaçlar.

### 6.4 Bot mesajları ertelenmiştir

Bot mesajlarının context'e eklenmesi **reddedilmiş bir özellik değildir**.

Şimdilik ertelenmiştir.

Ölçülecek soru:

> Önceki iki user turn ile çözülemeyen fakat bot cevabıyla güvenilir şekilde çözülebilen follow-up oranı anlamlı mı?

Anlamlıysa bot context kontrollü biçimde eklenebilir.

---

## 7. Academic Calendar V2

### 7.1 Veri yaşam döngüsü

Ürün kararı:

- Academic Calendar verisi her akademik yıl insan eliyle güncellenecektir.
- Sistem geçmiş yılların tüm takvimini bir arşiv ürünü gibi taşımak zorunda değildir.
- Aktif veri current academic year'ı temsil edecektir.
- Geçmiş yıl sorusuna güncel yılı yanlış cevaplamak yerine güvenli biçimde sonuç bulunamaması tercih edilir.

Takvim kayıtlarına akademik yıl bilgisinin eklenmesi veri doğrulama ve provenance açısından yine değerlidir.

### 7.2 Bahar / Güz

Bahar ve Güz kayıtlarını hard-disable etmek ilk tercih değildir.

Politika:

1. Kullanıcı açıkça dönem belirtiyorsa explicit term kullanılır.
2. Dönem belirtmiyorsa current term tercih edilir.
3. `GENERAL` / dönem-bağımsız event'ler desteklenebilir.
4. Bir dönemin aktif olmaması diğer döneme açıkça sorulan sorguyu görünmez yapmamalıdır.

Örnek:

> "Bütünleme sınavı ne zaman?"  
> → current term tercih edilir.

> "Güz bütünleme sınavı ne zamandı?"  
> → explicit `GUZ` current-term varsayımını override eder.

### 7.3 Intent Analyzer ile routing

Intent Analyzer her intent için:

```text
calendar_relevant = true | false
```

üretir.

İlk sürümde ayrıca `REQUIRED / SUPPLEMENTARY` gibi daha karmaşık bir sınıflandırma yapılmayacaktır.

Routing:

```text
calendar_relevant = false
→ QnA retrieval

calendar_relevant = true
→ QnA retrieval + filtered Calendar retrieval
```

### 7.4 Bütün Calendar selector'a verilmez

Mevcut "tüm Calendar kayıtlarını candidate pool'un başına ekle" davranışı V2 hedefinde kaldırılacaktır.

Calendar retrieval yalnız ilgili event kayıtlarını üretmelidir.

Takvim küçük olsa bile tasarım veri büyümesine dayanıklı olmalıdır.

Kavramsal filtreler:

- current academic year
- explicit term veya preferred current term
- event semantic/alias match
- validity / record activity
- küçük bounded result set

`calendar_relevant=true` fakat ilgili event bulunamazsa Calendar rastgele yakın bir event döndürmez:

```text
Calendar candidates = 0
```

Normal QnA hattı devam edebilir.

### 7.5 Calendar event eşleştirmesi

Calendar için ağır bir generative RAG zorunlu değildir.

Event kayıtlarında insan tarafından yönetilebilir alias/ifade listeleri bulunabilir.

Kesin retrieval yöntemi implementasyon/ölçüm aşamasında belirlenecektir.

---

## 8. QnA Retrieval

MeiliSearch ve Qdrant V2'de korunur.

Gerekçe:

- Retrieval mevcut sistemin en güçlü katmanlarından biridir.
- Gold candidate recall çok yüksektir.
- Yeni mimarinin amacı retrieval'ı yeniden icat etmek değildir.

Retrieval'ın görevi candidate üretmektir; final doğruluk kararı vermek değildir.

---

## 9. Candidate Eligibility

### 9.1 Tanım

Candidate eligibility şu soruya cevap verir:

> **Bu adayın selector tarafından değerlendirilmesine objektif olarak izin var mı?**

Şu soruya cevap vermez:

> "Bu aday kullanıcının sorusuna en iyi cevap mı?"

İkincisi selector'ın işidir.

### 9.2 V1 eligibility kriterleri

Başlangıç için muhafazakâr ve hard-rule temelli:

1. Candidate aktif mi?
2. Routing guard selector kullanımına izin veriyor mu?
3. Temporal/source validity sağlanıyor mu?
4. Bilgi kaynağı bu intent için açık mı?
5. Candidate desteklenmeyen/expired bir routing mode'da mı?

Objektif olarak elenemeyen aday selector'a bırakılır.

### 9.3 Eligibility'nin yapmaması gerekenler

V1'de eligibility:

- retrieval score threshold ile semantik cevap seçmez
- Qdrant rank'e göre candidate elemez
- Meili rank'e göre candidate elemez
- general/specific ayrımını hardcode etmez
- exact alias'ı mutlak truth kabul etmez
- keyword if/else ağına dönüşmez

Eligibility agresif olmamalıdır. Aksi halde mevcut yüksek retrieval recall sistem tarafından yanlışlıkla düşürülebilir.

---

## 10. Selector V2

### 10.1 Tek görev

> **Selector, kendisine verilen bounded ve eligible QnA candidate seti arasından intent'i güvenilir biçimde karşılayan tek bir curated QnA seçer; böyle bir candidate yoksa `NONE` döndürür.**

Selector:

- retrieval yapmaz
- intent split etmez
- Calendar routing kararı vermez
- cevap yazmaz
- candidate dışından QnA uydurmaz
- fallback politikası belirlemez

### 10.2 Selection yaklaşımı

Selector "en benzer adayı bul" şeklinde davranmamalıdır.

Tercih edilen zihniyet:

> **Candidate verification / semantic discrimination**

Her candidate için kavramsal olarak:

1. Konu gerçekten doğru mu?
2. Candidate kullanıcı ihtiyacının merkezini cevaplıyor mu?
3. Candidate kullanıcıdan daha spesifik bir koşulu varsayıyor mu?
4. Candidate kullanıcının açık koşullarıyla çelişiyor mu?
5. Candidate'ın seçilmesi için kullanıcıda bulunmayan bir varsayım gerekiyor mu?

### 10.3 General vs specific prensibi

> **Kullanıcının açıkça ifade etmediği bir qualifier varsayılarak daha spesifik QnA seçilmemelidir.**

### 10.4 Selector girdisi

Minimum:

- resolved intent
- candidate `qna_id`
- candidate canonical question
- candidate answer

Semantic metadata ileride opsiyonel olarak eklenebilir.

İlk V2 tasarımında modele zorunlu olarak verilmeyecek retrieval metadata:

- Qdrant score
- Meili score
- rank
- provider adı

### 10.5 Structured output

Selector output'u:

```json
{
  "decision": "SELECT",
  "qna_id": 342
}
```

veya:

```json
{
  "decision": "NONE"
}
```

Runtime selector'dan:

- confidence skoru
- reason code
- açıklama
- serbest reasoning metni

istenmeyecektir.

### 10.6 Candidate budget

Selector bounded candidate set almalıdır.

Kesin `K` değeri bu belgede dondurulmamıştır.

Candidate budget config üzerinden yönetilebilir ve benchmark ile belirlenecektir.

---

## 11. `NONE`, Hata ve Degraded Mode

### 11.1 Semantic `NONE`

`NONE`:

> **Selector çalıştı, eligible candidate setini değerlendirdi ve intent'i güvenilir biçimde karşılayan candidate bulamadı.**

Bu final semantik rettir.

`NONE` sonrası:

- Meili'den aynı adaylar threshold ile tekrar seçilmez
- Qdrant'tan aynı adaylar threshold ile tekrar seçilmez
- Calendar yeniden açılmaz

### 11.2 Ayrı sonuç türleri

```text
SELECT(qna_id)
NONE
SELECTOR_INVALID_OUTPUT
SELECTOR_ERROR
INTENT_ANALYZER_ERROR
LLM_OFF
LLM_UNAVAILABLE / DEGRADED
```

birbirine indirgenmemelidir.

### 11.3 Degraded mode

Admin paneldeki mevcut LLM ON/OFF mekanizması korunabilir ve V2'de resmi bir sistem modu haline getirilebilir.

LLM gerçekten sistemsel olarak unavailable olduğunda:

```text
Meili first
   ↓ miss
Qdrant
   ↓ miss
no answer
```

şeklindeki mevcut deterministik fallback yaklaşımı kullanılabilir.

Bu mod:

- semantic `NONE` sonrası tetiklenmez
- tek request timeout'unu otomatik global LLM_OFF saymak zorunda değildir
- admin LLM OFF, provider outage veya circuit-breaker benzeri sistemsel durumlarla ilişkilendirilmelidir

Kesin outage/circuit-breaker politikası implementasyon planında detaylandırılacaktır.

---

## 12. Final Answer Composition

Mevcut deterministik composition yaklaşımı korunabilir.

Single intent:

- seçilen curated answer verbatim

Multi intent:

- iki seçilen answer
- exact duplicate ise dedupe
- belirlenmiş sırada deterministik birleştirme

Yeni generative composition çağrısı eklenmez.

---

## 13. Model ve Provider Yönetimi

### 13.1 Capability bazlı config

"Chatbot modeli" tek global kavram olmamalıdır.

En az iki capability:

```text
intent_analyzer
selector
```

Her capability bağımsız olarak şu ayarlara sahip olabilir:

- provider
- model
- reasoning effort
- temperature
- max tokens
- timeout
- retry policy
- structured-output capability/config

Intent Analyzer ve Selector aynı modeli kullanabilir fakat zorunlu değildir.

### 13.2 Hardcoded model isimleri kaldırılacaktır

Model davranışı configuration olmalıdır.

### 13.3 Model registry / allowlist

Admin panel serbest model string'i kabul etmemelidir.

Registry en az:

```text
model_id
provider
allowed_capabilities
structured_output_support
reasoning_support
allowed_reasoning_efforts
enabled
```

bilgilerini taşıyabilir.

### 13.4 Admin rolü

Model/config değiştirme yetkisi sınırlı rol/izin ile korunmalıdır.

Kesin rol adı mevcut authorization modeline göre implementasyon aşamasında seçilecektir.

### 13.5 Config versioning ve rollback

Her production AI-config değişikliği:

- kim yaptı
- eski değer
- yeni değer
- zaman
- config version

ile audit edilmelidir.

Önceki config'e rollback mümkün olmalıdır.

### 13.6 Model qualification

Yeni model production'da seçilebilir hale gelmeden önce capability contract testlerinden geçmelidir.

Reasoning-effort değişikliği de davranış değişikliği kabul edilmeli ve ölçülmelidir.

---

## 14. Observability

Observability V2 için opsiyonel değildir.

Amaç:

> Model/provider/config değiştiğinde sistem davranışındaki değişiklik production'da ölçülebilir olmalıdır.

### 14.1 Decision trace

Her request için PII kurallarına uygun bir decision trace üretilmelidir.

En az:

**Genel**
- request_id
- conversation_id veya güvenli referansı
- deployment/git version
- AI config version
- LLM mode

**Intent Analyzer**
- provider
- model
- reasoning effort
- intent_count
- context_used
- calendar_relevant
- latency
- token usage
- retry count
- error/invalid output

**Retrieval**
- Meili candidate count
- Qdrant candidate count
- Calendar candidate count
- eligible candidate IDs
- candidate ordering

**Selector**
- provider
- model
- reasoning effort
- candidate QnA IDs
- decision
- selected QnA ID veya NONE
- latency
- token usage
- retry count
- ERROR / INVALID_OUTPUT

**Final**
- selected source type
- selected qna_ids
- degraded mode kullanıldı mı
- response outcome

### 14.2 PII

Raw hassas veri observability loglarına kontrolsüz biçimde düşmemelidir.

### 14.3 Operasyonel metrikler

Intent Analyzer:
- SINGLE oranı
- MULTI oranı
- analyzer error/invalid-output oranı
- context_used oranı
- calendar_relevant oranı

Selector:
- SELECT oranı
- NONE oranı
- ERROR oranı
- INVALID_OUTPUT oranı

LLM/provider:
- latency p50/p95
- token usage
- retry rate
- provider error rate
- rate-limit oranı

Routing:
- Calendar candidate üretim oranı
- degraded-mode kullanım oranı
- final no-answer oranı

### 14.4 Davranış alarmı

Model/config değişikliklerinden sonra dağılımlardaki belirgin sapmalar regression sinyali olarak gözlenmelidir.

Kesin alarm eşikleri gerçek production baseline'ından sonra belirlenecektir.

---

## 15. Semantic Metadata Kararı

Semantic metadata şu anda zorunlu V2 gereksinimi değildir.

Near-QnA conflict kümeleri metadata'nın yardımcı olabileceğine dair kanıt üretmiştir, ancak 326 QnA'nın tamamına metadata eklenmesinin gerekli olduğu kanıtlanmamıştır.

Karar:

> **Metadata gerekliliği deneyle doğrulanmadan zorunlu KB şemasına dönüşmeyecektir.**

Önerilen necessity experiment:

1. Mevcut selector
2. Daha sıkı selector contract, metadata yok
3. Yalnız problemli QnA kümelerine minimal metadata

Karşılaştırılabilecek minimal metadata:

- scope
- qualifiers
- excludes

---

## 16. Exact Alias Kararı

Exact alias güçlü bir deterministic sinyaldir ancak mutlak truth değildir.

Gold review sırasında exact alias owner ile Gold arasında gerçek çatışmalar görülmüştür.

Bu nedenle V2 başlangıcında:

- exact alias bypass zorunlu değildir
- `exact string equality = truth` kabul edilmez
- exact alias'ın selector öncesi veya selector içi nasıl kullanılacağı ayrı deney gerektirir

---

## 17. Açık / Ertelenmiş Kararlar

Aşağıdakiler bilinçli olarak bu belgede dondurulmamıştır:

1. Selector candidate budget (`K`)
2. Bot mesajlarının Intent Analyzer context'ine eklenmesi
3. Semantic metadata'nın gerekliliği ve kapsamı
4. Exact alias'ın karar hattındaki rolü
5. Intent Analyzer ve Selector için en iyi model
6. Reasoning effort seviyesi
7. Calendar retrieval'ın kesin matching algoritması
8. 3+ intent mesajlarında UX
9. Partial multi-intent cevap UX'i
10. Request-level model error ile global degraded-mode arasındaki circuit-breaker politikası
11. 17 context-required Gold vakanın expected target annotation'ı
12. Model/provider bazlı production alarm eşikleri

Bunlar unutulmuş değildir; veri veya deney gerektirdikleri için ertelenmiştir.

---

## 18. Korunacak Mevcut Güçlü Parçalar

V2 bir rewrite değildir.

Mümkün olduğunca korunması beklenen alanlar:

- FastAPI endpoint/service sınırları
- curated QnA answer yaklaşımı
- MeiliSearch retrieval
- Qdrant retrieval
- qna_id tabanlı dedupe
- routing guard fail-closed semantiği
- QnA CRUD → Meili/Qdrant senkronizasyonu
- conversation ownership token yaklaşımı
- bounded conversation storage
- provider adapter sınırı
- deterministic final answer composition
- admin LLM ON/OFF fikri
- Meili-first / Qdrant degraded-mode fikri

---

## 19. Kaldırılması / Yeniden Tasarlanması Hedeflenen Davranışlar

1. Her mesajda çalışan sınırsız splitter
2. Splitter'ın serbest semantic reformulation yapması
3. Multi-intent durumda atılan spekülatif full-query selector çağrısı
4. Tüm Calendar kayıtlarının bütün selector promptlarına eklenmesi
5. Calendar'ın akademik yıl/term/eligibility olmadan candidate olması
6. Semantic `NONE` sonrası Meili/Qdrant threshold fallback
7. `NONE`, malformed output ve model error'ın semantik olarak karışması
8. Context'in pipeline'ın farklı aşamalarında farklı biçimde kullanılması
9. Hardcoded model isimleri ve eksik runtime model config
10. Model/provider/reasoning değişikliklerinin yeterince audit edilmemesi
11. Production decision provenance eksikliği
12. Guard uygulanmadan gösterilen suggestion davranışı ayrıca güvenlik/eligibility açısından gözden geçirilmelidir

---

## 20. Model-Independent Capability Yaklaşımı

V2'nin uzun vadeli hedefi:

```text
AUZEF Chatbot = belirli bir model
```

değildir.

Hedef:

```text
AUZEF Chatbot
  ├── Intent Analyzer capability
  ├── Retrieval capability
  ├── Calendar retrieval capability
  ├── Selector capability
  ├── Guard policy
  ├── Composition
  └── Observability
```

Bir model kaldırılırsa veya provider değişirse yalnız ilgili capability'nin yeni modeli contract benchmarkından geçirilir.

Architecture yeniden yazılmaz.

---

## 21. Geçiş Stratejisi

Implementasyon tek büyük değişiklik halinde yapılmamalıdır.

### Faz 0 — Baseline'i koru

- Frozen Gold / Session Gold
- Reviewed Gold v2
- Existing benchmark traces
- Exact metriği primary metric
- KB overlap flagged segmentini ayrı raporlama

### Faz 1 — Observability ve model config temeli

Davranış değiştirmeden:

- config abstraction
- capability config
- trace schema
- model/config versioning
- ERROR/NONE ayrımı için temel tipler

### Faz 2 — Intent Analyzer

- max 2
- single default
- minimal normalization
- previous 2 user turns
- resolved_text
- calendar_relevant

A/B benchmark yapılır.

### Faz 3 — Calendar V2

- unconditional Calendar injection kaldırılır
- Calendar relevance routing
- academic year / term policy
- filtered event retrieval
- no-match fail-safe

### Faz 4 — Candidate eligibility + Selector V2

- bounded eligible candidate set
- structured SELECT/NONE
- semantic NONE final
- general/specific contract
- no confidence / reason code
- invalid/error ayrımı

### Faz 5 — Degraded mode

- admin LLM OFF
- provider/system unavailable policy
- request error vs global degraded ayrımı
- Meili-first / Qdrant fallback semantics

### Faz 6 — Model registry / admin control

- allowlist
- capability-based model selection
- reasoning config
- role permissions
- audit log
- config rollback

### Faz 7 — Necessity experiments

- metadata
- bot context
- candidate K
- exact alias
- reasoning-effort sensitivity

Her faz kendi benchmarkını geçmeden sonraki davranışsal faza bağlanmamalıdır.

---

## 22. Ölçüm ve Acceptance Kriterleri

### 22.1 Ana kalite metriği

**Exact E2E accuracy** primary metric'tir.

Relaxed/coverage secondary diagnostic metric olarak tutulabilir.

### 22.2 Retrieval

V2 değişiklikleri retrieval recall'ı anlamlı biçimde düşürmemelidir.

### 22.3 Intent Analyzer

Ölçülecek:

- false split
- missed multi-intent
- semantic expansion
- normalized/resolved fidelity
- context resolution
- irrelevant context contamination
- Calendar relevance false positive / false negative
- structured output failure
- latency/cost

### 22.4 Selector

Ölçülecek:

- exact accuracy
- conditional accuracy
- general/specific confusion
- near-duplicate confusion
- NONE rate
- false NONE
- invalid output
- model error
- latency/cost

### 22.5 Calendar

Ölçülecek:

- Calendar relevance precision/recall
- doğru event retrieval
- current-term default doğruluğu
- explicit-term override
- unrelated Calendar candidate sayısı
- Calendar kaynaklı final yanlış cevap

### 22.6 Model portability

Yeni model veya reasoning config, capability contract ve latency/error testlerini geçmeden production'da selectable olmamalıdır.

---

## 23. Test Sınıfları

### Intent
- single normal mesaj
- iki bağımsız intent
- iki cümle ama tek intent
- bağlam cümlesi + ana talep
- 3+ talep
- bozuk Türkçe
- semantic expansion engeli

### Context
- gerçek follow-up
- irrelevant previous topic
- topic switch
- pronoun/reference
- user-only context yetersiz vaka
- iki intentli follow-up

### Calendar
- tarih isteyen soru
- "nasıl" sorusu
- term belirtilmeyen event
- explicit Güz
- explicit Bahar
- geçmiş yıl sorusu
- calendar_relevant true ama event yok
- QnA + Calendar birlikte yararlı vaka

### Selector
- obvious match
- no match
- general vs specific
- near duplicate
- acceptable multiple candidates
- inactive/guarded candidate
- malformed model output
- timeout/error

### Mode
- LLM ON
- admin LLM OFF
- provider unavailable
- request-level transient error
- semantic NONE

---

## 24. Karar Kaydı

| Konu | Karar | Durum |
|---|---|---|
| Splitter | Yerine Intent Analyzer | Donduruldu |
| Intent sayısı | 1 veya maksimum 2 | Donduruldu |
| Default | SINGLE / belirsizlikte SINGLE | Donduruldu |
| Reformülasyon | Minimal; semantic expansion yasak | Donduruldu |
| Context | Önceki 2 user turn | V1 için donduruldu |
| Bot context | İlk sürümde yok | Deney sonrası |
| Calendar routing | Intent Analyzer `calendar_relevant` | Donduruldu |
| Calendar data | Current-year, insan eliyle güncel | Donduruldu |
| Bahar/Güz | Explicit term override, aksi halde current term preferred | Donduruldu |
| Calendar candidates | Tüm tablo değil, filtreli event retrieval | Donduruldu |
| QnA retrieval | Meili + Qdrant korunur | Donduruldu |
| Eligibility | Hard/objective filter | Donduruldu |
| Selector | LLM semantic verifier | Donduruldu |
| Selector output | Structured SELECT(qna_id) / NONE | Donduruldu |
| Confidence | İstenmeyecek | Donduruldu |
| Reason code | Runtime LLM'den istenmeyecek | Donduruldu |
| Semantic NONE | Final; retrieval fallback yok | Donduruldu |
| LLM OFF | Ayrı degraded mode | Yön donduruldu, operasyon detayı açık |
| Metadata | Zorunlu değil | Experiment |
| Exact alias | Mutlak bypass değil | Experiment |
| Model config | Capability-based, hardcode dışı | Donduruldu |
| Admin model change | Allowlist + role + audit + rollback | Donduruldu |
| Observability | Zorunlu | Donduruldu |
| Primary metric | Exact E2E | Donduruldu |

---

## 25. Nihai Mimari Tezi

AUZEF Answer Pipeline V2'nin temel tezi:

> **Dil anlamlandırma ve yakın adaylar arasındaki semantik ayrım LLM'in güçlü olduğu alanlardır; fakat routing, validity, eligibility, model failure semantiği ve provenance gibi sistem kararları LLM'e bırakılmamalıdır.**

Bunun sonucu olarak:

- Intent Analyzer kullanıcı mesajını dar bir sözleşmeyle anlamlandırır.
- Retrieval doğru adayları getirir.
- Calendar yalnız ilgili intent'lerde devreye girer.
- Eligibility objektif olarak kullanılamaz adayları eler.
- Selector kalan semantik belirsizliği çözer.
- `NONE` semantik olarak anlamlı ve final bir karardır.
- LLM-off ayrı bir sistem modudur.
- Curated cevaplar korunur.
- Model değişikliği architecture değişikliği değil configuration değişikliğidir.
- Production davranışı observability ile ölçülür.

Bu belge implementasyon ajanlarının mimari kaynak belgesidir. Bir implementasyon kararı bu sözleşmelerden birini değiştirecekse, değişiklik kod seviyesinde sessizce yapılmamalı; önce mimari karar olarak yeniden değerlendirilmelidir.

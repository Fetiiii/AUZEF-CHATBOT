# AUZEF Chatbot - Çözüm Merkezi Entegrasyonu Teknik Spesifikasyonu

> Version: 1.0
>
> Amaç: AUZEF Chatbot içerisine Çözüm Merkezi talep oluşturma entegrasyonunun production seviyesinde geliştirilmesi.

---

# Genel Amaç

Kullanıcı chatbot üzerinden:

- TC Kimlik numarasını doğrulayacak.
- SMS doğrulaması yapacak.
- Gerekirse öğrenci seçimi yapacak.
- Kategori seçecek.
- Talep mesajını yazacak.
- Talep doğrudan Çözüm Merkezi sistemine oluşturulacak.

Chatbot yalnızca istemci (client) görevi görecektir.

Tüm iş mantığı Çözüm Merkezi API'leri üzerinden ilerleyecektir.

---

# Genel Mimari

```
Kullanıcı
      │
      ▼
Chatbot
      │
      ▼
SolutionCenterService
      │
      ▼
SolutionCenterClient
      │
      ▼
Çözüm Merkezi API
```

Kurallar

- Chatbot HTTP isteği atmayacak.
- HTTP çağrıları yalnızca Client katmanında olacak.
- İş mantığı yalnızca Service katmanında olacak.

---

# Proje Yapısı

```
app/

├── integrations/
│
│   └── solution_center/
│
│       ├── __init__.py
│       ├── base_client.py
│       ├── client.py
│       ├── service.py
│       ├── mapper.py
│       ├── category_cache.py
│       ├── models.py
│       ├── schemas.py
│       ├── exceptions.py
│       └── constants.py
```

---

# Dosya Sorumlulukları

## base_client.py

- httpx.AsyncClient
- Authorization Header
- Timeout
- Retry
- Base URL

---

## client.py

Sadece HTTP çağrıları.

Hiç iş mantığı bulunmayacak.

Public metodlar

```
get_phone()

send_sms()

verify_code()

get_categories()

create_ticket()
```

---

## service.py

Chatbot yalnızca burayı kullanacak.

Public metodlar

```
start_verification()

verify_otp()

select_student()

get_categories()

create_ticket()
```

---

## mapper.py

Kategori listesini

Flat JSON

↓

Tree

şeklinde dönüştürecek.

---

## category_cache.py

Kategori listesini memory içerisinde cache edecek.

TTL

15 dakika

---

## models.py

Pydantic modelleri

---

## schemas.py

API Request / Response modelleri

---

## exceptions.py

Tüm özel exception sınıfları.

---

# Authorization

API iki farklı token kullanmaktadır.

---

## 1 Service Token

Swagger Authorize kısmına girilen token.

Bu token

- Sabittir.
- Backend config içerisinde tutulacaktır.
- Tüm API isteklerinde Authorization header olarak gönderilecektir.

```
Authorization: Bearer <CM_SERVICE_TOKEN>
```

Bu token kullanıcı sessionında tutulmayacaktır.

---

## 2 verificationToken

SMS gönderildikten sonra üretilmektedir.

Bu token

- Kullanıcıya özeldir.
- SMS doğrulama oturumunu temsil eder.
- Talep oluştururken tekrar kullanılmaktadır.

verificationToken session içerisinde tutulacaktır.

---

# API Endpointleri

---

## Telefon Bilgisi Getir

POST

```
/service/chatbotapi/v1/kimlik-ile-telefon-al
```

Headers

```
Authorization: Bearer SERVICE_TOKEN
```

Request

```json
{
  "kimlikNo": "11111111111"
}
```

Response

```json
{
  "isSuccess": true,
  "code": 200,
  "data": {
    "maskedPhoneNumber": "*******208"
  },
  "message": "Telefon numarası getirildi."
}
```

---

## SMS Gönder

POST

```
/service/chatbotapi/v1/telefona-dogrulama-kodu-gonder
```

Request

```json
{
  "kimlikNo": "11111111111"
}
```

Response

```json
{
  "isSuccess": true,
  "code": 200,
  "data": {
    "verificationToken": "0010b681..."
  }
}
```

verificationToken session içerisine yazılır.

---

## OTP Doğrula

POST

```
/service/chatbotapi/v1/dogrulama-kodu-ile-ogrenci-bilgisi-al
```

Request

```json
{
  "verificationCode": "920562",
  "verificationToken": "0010b681..."
}
```

Response

```json
{
  "isSuccess": true,
  "code": 200,
  "data": [
    {
      "ogrenciId": 649203,
      "birimAdi": "...",
      "fakulteAdi": "..."
    }
  ]
}
```

Not

Aynı TC altında birden fazla öğrenci bulunabilir.

---

## Tüm Kategorileri Getir

POST

```
/service/chatbotapi/v1/tum-kategorileri-al
```

Request

```json
{}
```

Response

```json
{
  "uuid": "...",
  "shortCode": "...",
  "name": "...",
  "isLeaf": false,
  "sortOrder": 1,
  "parentUuid": null
}
```

API flat liste döndürmektedir.

Mapper tarafından tree yapısına dönüştürülecektir.

---

## Talep Oluştur

POST

```
/service/chatbotapi/v1/talep-olustur
```

Request

```json
{
  "verificationToken": "...",

  "channel": {
    "shortCode": "AUZEF_WEB_SAYFASI_CHATBOT"
  },

  "category": {
    "shortCode": "..."
  },

  "ogrenciId": 649203,

  "description": "Talep açıklaması"
}
```

Response

```json
{
  "isSuccess": true,
  "code": 200,
  "data": {
    "id": 2050995,
    "uuid": "BMGJRL"
  }
}
```

---

# Chatbot Akışı

```
Talep Oluştur

↓

TC Gir

↓

Telefonu Göster

↓

SMS Gönder

↓

verificationToken Session'a Yaz

↓

OTP Gir

↓

Öğrencileri Getir

↓

Birden fazla öğrenci varsa seçim yaptır

↓

Kategorileri Göster

↓

Alt kategori seçtir

↓

Talep metni al

↓

Talep oluştur

↓

Ticket UUID göster
```

---

# State Machine

```
IDLE

↓

SC_WAIT_TC

↓

SC_WAIT_OTP

↓

SC_WAIT_STUDENT_SELECTION

↓

SC_WAIT_CATEGORY_SELECTION

↓

SC_WAIT_DESCRIPTION

↓

SC_CREATING_TICKET

↓

SC_FINISHED
```

---

# Session Yapısı

```json
{
  "solution_center": {

    "verified": true,

    "verificationToken": "...",

    "students": [

    ],

    "selectedStudentId": 649203
  }
}
```

---

# Pydantic Modelleri

## Student

```
ogrenciId

birimAdi

fakulteAdi
```

---

## Category

```
uuid

shortCode

name

isLeaf

sortOrder

parentUuid
```

---

## TicketCreateRequest

```
verificationToken

channel

category

ogrenciId

description
```

---

# Kategori Cache

Kategori endpointi her kullanıcı için tekrar çağrılmayacaktır.

İlk istek

↓

API

↓

Memory Cache

↓

15 dakika TTL

↓

Sonraki kullanıcılar Memory üzerinden okuyacaktır.

---

# Mapper

Kategori endpointi flat liste döndürmektedir.

Örneğin

```
Öğrenci İşleri

Yeni Kayıt

Ders Kaydı

Sınav
```

Mapper çıktısı

```
Öğrenci İşleri

├── Yeni Kayıt

├── Ders Kaydı

└── Sınav
```

Chatbot yalnızca

```
isLeaf == true
```

olan kategorilerin seçilmesine izin verecektir.

---

# Exception Yapısı

```
SolutionCenterException

├── InvalidOtpException

├── StudentNotFoundException

├── TicketCreateException

├── ApiConnectionException

├── UnauthorizedException

└── VerificationExpiredException
```

---

# Loglama

Loglanacak

- Endpoint
- HTTP Status
- Response Time
- Conversation ID
- Ticket UUID

Loglanmayacak

- TC Kimlik
- verificationToken
- OTP
- Service Token

---

# Güvenlik

- verificationToken frontend'e gönderilmeyecek.
- Service Token yalnızca backend tarafından kullanılacak.
- SSL doğrulaması açık olacak.
- Timeout kullanılacak.
- HTTP istekleri async olacak.

---

# Test Senaryoları

- Geçerli TC
- Geçersiz TC
- Yanlış OTP
- Süresi dolmuş verificationToken
- Birden fazla öğrenci kaydı
- Kategori bulunamadı
- Talep başarıyla oluşturuldu
- API Timeout
- API 500 hatası

---

# Claude Code Kuralları

Bu modül production ortamında kullanılacaktır.

Kurallar

- Python 3.11+
- FastAPI
- httpx.AsyncClient
- Pydantic v2
- SOLID prensipleri
- Type Hint zorunlu
- Async yapı korunacak
- Dependency Injection kullanılacak
- Magic string kullanılmayacak
- İş mantığı yalnızca service.py içerisinde olacak.
- HTTP çağrıları yalnızca client.py içerisinde olacak.
- Kod test edilebilir olacak.
- Kategori cache thread-safe olacak.
- Tüm modeller strongly typed olacak.

---

# Geliştirme Checklist'i

- [ ] BaseClient oluştur
- [ ] Authorization yapısını ekle
- [ ] Pydantic modellerini oluştur
- [ ] API Client metodlarını yaz
- [ ] Service katmanını oluştur
- [ ] Kategori mapper'ını yaz
- [ ] Memory cache ekle
- [ ] Exception yapısını oluştur
- [ ] State Machine entegrasyonu
- [ ] Ticket oluşturma akışını tamamla
- [ ] Loglama
- [ ] Unit testler

---

# Notlar

- `verificationToken`, SMS gönderme endpointinden dönen token olup, OTP doğrulaması sonrasında da aynı token talep oluşturma işleminde kullanılacaktır.
- Aynı TC Kimlik numarasına birden fazla öğrenci kaydı bağlı olabilir; kullanıcıdan seçim alınmalıdır.
- Kategoriler dinamik olarak API'den alınacak, uygulama içinde hardcode edilmeyecektir.
- `channel.shortCode` değeri chatbot kanalına uygun sabit değer (ör. `AUZEF_WEB_SAYFASI_CHATBOT`) olarak gönderilecektir.
- API'den dönen hata mesajları doğrudan kullanıcıya gösterilmemeli, uygulama içinde anlamlı exception'lara dönüştürülmelidir.
from abc import ABC, abstractmethod
import meilisearch
from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct, Distance, VectorParams
from sentence_transformers import SentenceTransformer

#: Alias noktalarının kimlik aralığı. `qna.id` ile çakışmaması için ofsetli;
#: u64 sınırının çok altında kalıyor.
ALIAS_ID_OFFSET = 1_000_000_000
#: Kayıt başına indekslenecek azami alias. Import biçimi 20 sütun taşıyor;
#: tavan onun üstünde tutuldu ki elle eklenenler de girsin.
MAX_ALIASES_PER_QNA = 64
#: Tek upsert isteğine giren azami nokta.
UPSERT_BATCH = 256


def _usable_aliases(queries) -> list:
    """Boş/yinelenen alias'ları eler, tavanı uygular.

    Sıra korunuyor: aynı girdi aynı nokta kimliklerini üretmeli.
    """
    if not queries:
        return []
    seen = set()
    result = []
    for raw in queries:
        text_value = (raw or "").strip()
        if not text_value or text_value.casefold() in seen:
            continue
        seen.add(text_value.casefold())
        result.append(text_value)
        if len(result) >= MAX_ALIASES_PER_QNA - 1:
            break
    return result


class BaseSearchProvider(ABC):
    @abstractmethod
    def healthcheck(self):
        """Servis-seviyesi ucuz erişilebilirlik kontrolü."""
        pass

    @abstractmethod
    def search(self, query: str, limit: int = 3):
        pass

class MeiliSearchProvider(BaseSearchProvider):
    def __init__(self, url: str, master_key: str, index_name: str):
        self.client = meilisearch.Client(url, master_key)
        self.index = self.client.index(index_name)

    def healthcheck(self):
        """Arama yapmadan MeiliSearch health API'sini doğrula."""
        health = self.client.health()
        if health.get("status") != "available":
            raise RuntimeError("MeiliSearch health status is not available")

    def search(self, query: str, limit: int = 3):
        results = self.index.search(query, {
            'limit': limit,
            'showRankingScore': True
        })
        return [
            {
                "id": hit['id'],
                "qna_id": hit['id'],
                "question": hit['question'],
                "answer": hit['answer'],
                "score": hit.get('_rankingScore', 0),
                "source": "meilisearch"
            } for hit in results['hits']
        ]

    def get_suggestions(self, query: str, limit: int = 3):
        results = self.index.search(query, {
            'limit': limit,
            'matchingStrategy': 'last'
        })
        return [hit['question'] for hit in results['hits']]

    def get_suggestion_hits(self, query: str, limit: int = 3):
        """Suggestion titles WITH their QnA ids so callers can apply the
        routing-guard/activity rules before showing any title."""
        results = self.index.search(query, {
            'limit': limit,
            'matchingStrategy': 'last'
        })
        return [
            {"qna_id": hit.get('id'), "question": hit.get('question')}
            for hit in results['hits']
        ]

    def add_documents(self, documents: list):
        self.index.add_documents(documents)

    def update_documents(self, documents: list):
        self.index.update_documents(documents)

    def delete_document(self, document_id: int):
        self.index.delete_document(document_id)

class QdrantProvider(BaseSearchProvider):
    def __init__(self, host: str, port: int, collection_name: str, model_name: str):
        self.client = QdrantClient(host=host, port=port)
        self.collection_name = collection_name
        self.model = SentenceTransformer(model_name)

    def healthcheck(self):
        """Embedding üretmeden Qdrant servis metadata'sını oku."""
        self.client.get_collections()

    def ensure_collection(self):
        try:
            self.client.get_collection(self.collection_name)
        except Exception:
            vector_size = self.model.get_sentence_embedding_dimension()
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
            )

    def search(self, query: str, limit: int = 3):
        query_vector = self.model.encode(query).tolist()
        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=limit
        )
        return [
            {
                "id": point.id,
                "qna_id": point.payload.get('qna_id'),
                "question": point.payload.get('question'),
                "answer": point.payload.get('answer'),
                "score": point.score,
                "source": "qdrant",
                "matched_query": point.payload.get('matched_query'),
            } for point in results.points
        ]

    def _alias_point_id(self, qna_id: int, position: int) -> int:
        """Alias noktasının DETERMİNİSTİK kimliği.

        Kanonik nokta kimliği `qna.id` olarak KALIYOR; alias'lar ofsetli bir
        aralığa yazılıyor. Deterministik olması şart: aksi hâlde her senkron
        yeni nokta üretir ve indeks eski alias'larla şişerdi.
        """
        return ALIAS_ID_OFFSET + qna_id * MAX_ALIASES_PER_QNA + position

    def _points_for(self, qna_id: int, question: str, answer: str, vectors, queries):
        """Bir kaydın kanonik + alias noktaları.

        ALIAS'LAR NEDEN AYRI NOKTA: öğrenci "Sınav kitapçığı" yazıyor, KB'de
        ise cilalı "Çıkmış sınav sorularına nereden ulaşabilirim?" duruyor.
        Yalnız kanonik cümle vektörlenirse dağınık sorgunun ona 0.75 üstü
        benzemesi gerekiyor. Alias da vektörlenince sorgu, cilalı cümleye
        değil BAŞKA BİR DAĞINIK İFADEYE yakın düşüyor.

        Payload'da `question`/`answer` kaydın KENDİ metinleri kalıyor: arama
        hattı bu iki anahtarı okuyor ve kullanıcıya alias değil kanonik soru
        gösterilmeli.
        """
        points = [
            PointStruct(
                id=qna_id,
                vector=vectors[0].tolist() if hasattr(vectors[0], "tolist") else list(vectors[0]),
                payload={"question": question, "answer": answer, "qna_id": qna_id},
            )
        ]
        for position, (alias, vec) in enumerate(zip(queries, vectors[1:]), start=1):
            points.append(
                PointStruct(
                    id=self._alias_point_id(qna_id, position),
                    vector=vec.tolist() if hasattr(vec, "tolist") else list(vec),
                    payload={
                        "question": question,
                        "answer": answer,
                        "qna_id": qna_id,
                        # Hangi ifadenin eşleştiği ayıklamada gerekiyor.
                        "matched_query": alias,
                    },
                )
            )
        return points

    def upsert_point(self, point_id: int, question: str, answer: str, queries: list = None):
        queries = _usable_aliases(queries)
        vectors = self.model.encode([question] + queries)
        self.client.upsert(
            collection_name=self.collection_name,
            points=self._points_for(point_id, question, answer, vectors, queries),
        )

    def upsert_points(self, items: list):
        """Toplu upsert. items: [(id, question, answer, queries?), ...].

        Metinlerin TAMAMINI (kanonik + alias) tek batch'te encode eder ve tek
        upsert çağrısıyla yükler — CSV import'ta satır satır encode + HTTP
        yerine (500 kayıt = 500 encode/istek) tek encode + tek istek."""
        if not items:
            return
        normalized = [
            (item[0], item[1], item[2], _usable_aliases(item[3] if len(item) > 3 else None))
            for item in items
        ]
        # Tek düz liste: model bir kez çağrılır, sonra kayıt başına dilimlenir.
        flat = []
        for _id, question, _answer, queries in normalized:
            flat.append(question)
            flat.extend(queries)
        vectors = self.model.encode(flat)

        points = []
        cursor = 0
        for _id, question, answer, queries in normalized:
            span = 1 + len(queries)
            points.extend(
                self._points_for(_id, question, answer, vectors[cursor : cursor + span], queries)
            )
            cursor += span
        # Parçalı: CSV import'ta kayıt başına ~20 alias ile nokta sayısı
        # hızla büyüyor ve tek istek Qdrant'ta zaman aşımına düşüyor.
        for start in range(0, len(points), UPSERT_BATCH):
            self.client.upsert(
                collection_name=self.collection_name,
                points=points[start : start + UPSERT_BATCH],
            )

    def delete_point(self, point_id: int):
        """Kaydı VE alias noktalarını siler.

        Yalnız kanonik noktayı silmek, alias'ları indekste öksüz bırakır ve
        silinmiş bir kaydın cevabı verilmeye devam ederdi.
        """
        alias_ids = [
            self._alias_point_id(point_id, position)
            for position in range(1, MAX_ALIASES_PER_QNA)
        ]
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=[point_id, *alias_ids],
        )

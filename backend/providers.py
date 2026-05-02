from abc import ABC, abstractmethod
import meilisearch
from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct, Distance, VectorParams
from sentence_transformers import SentenceTransformer

class BaseSearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, limit: int = 3):
        pass

class MeiliSearchProvider(BaseSearchProvider):
    def __init__(self, url: str, master_key: str, index_name: str):
        self.client = meilisearch.Client(url, master_key)
        self.index = self.client.index(index_name)

    def search(self, query: str, limit: int = 3):
        results = self.index.search(query, {
            'limit': limit,
            'showRankingScore': True
        })
        return [
            {
                "id": hit['id'],
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
                "question": point.payload.get('question'),
                "answer": point.payload.get('answer'),
                "score": point.score,
                "source": "qdrant"
            } for point in results.points
        ]

    def upsert_point(self, point_id: int, question: str, answer: str):
        vector = self.model.encode(question).tolist()
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "question": question,
                "answer": answer
            }
        )
        self.client.upsert(
            collection_name=self.collection_name,
            points=[point]
        )

    def delete_point(self, point_id: int):
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=[point_id]
        )

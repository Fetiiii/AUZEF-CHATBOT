from abc import ABC, abstractmethod
import meilisearch
from qdrant_client import QdrantClient
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

class QdrantProvider(BaseSearchProvider):
    def __init__(self, host: str, port: int, collection_name: str, model_name: str):
        self.client = QdrantClient(host=host, port=port)
        self.collection_name = collection_name
        self.model = SentenceTransformer(model_name)

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

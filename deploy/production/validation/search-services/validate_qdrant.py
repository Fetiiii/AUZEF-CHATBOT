#!/usr/bin/env python3
"""Validate the AUZEF Qdrant client contract against a real server.

This validator is deliberately destructive only to a UUID-suffixed transient
collection. It never opens, mutates, or removes the production application
collection (``auzef_qna_vectors``).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time
import types
from urllib.request import Request, urlopen
import uuid
import warnings


EXPECTED_CLIENT_VERSION = "1.19.0"
EXPECTED_SERVER_VERSION = os.getenv("QDRANT_EXPECTED_VERSION", "1.19.1")
APPLICATION_COLLECTION = "auzef_qna_vectors"
VECTOR_SIZE = 8

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

# providers.py imports SentenceTransformer at module load. The protocol test
# injects _DeterministicModel instead of downloading production model weights.
sentence_transformers_stub = types.ModuleType("sentence_transformers")
sentence_transformers_stub.SentenceTransformer = object
sys.modules.setdefault("sentence_transformers", sentence_transformers_stub)

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.http.models import Distance  # noqa: E402
import services.providers as application_providers  # noqa: E402
from services.providers import QdrantProvider  # noqa: E402


class _Vector(list[float]):
    """A tiny ndarray-like value matching the provider's ``tolist`` use."""

    def tolist(self) -> list[float]:
        return list(self)


class _DeterministicModel:
    """Exercise the application provider without downloading model weights."""

    def get_sentence_embedding_dimension(self) -> int:
        return VECTOR_SIZE

    @staticmethod
    def _encode_one(text: str) -> _Vector:
        encoded = text.encode("utf-8") or b"\0"
        values = [
            float(encoded[index % len(encoded)] + index + 1)
            for index in range(VECTOR_SIZE)
        ]
        norm = math.sqrt(sum(value * value for value in values))
        return _Vector(value / norm for value in values)

    def encode(self, value: str | list[str]):
        if isinstance(value, str):
            return self._encode_one(value)
        return [self._encode_one(item) for item in value]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate qdrant-client 1.19.0 against Qdrant Server 1.19.1",
    )
    parser.add_argument("--host", default=os.getenv("QDRANT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    parser.add_argument("--api-key", default=os.getenv("QDRANT_API_KEY"))
    parser.add_argument(
        "--https",
        action="store_true",
        default=os.getenv("QDRANT_HTTPS", "").strip().lower() == "true",
        help="Use HTTPS for both REST metadata and SDK calls",
    )
    parser.add_argument(
        "--expected-server-version",
        default=EXPECTED_SERVER_VERSION,
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args()


def _http_get(
    host: str,
    port: int,
    path: str,
    api_key: str | None,
    timeout: float,
    https: bool,
):
    headers = {"api-key": api_key} if api_key else {}
    scheme = "https" if https else "http"
    request = Request(f"{scheme}://{host}:{port}{path}", headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit test target
        body = response.read().decode("utf-8")
        if response.status != 200:
            raise AssertionError(f"GET {path} returned HTTP {response.status}")
        return body


def _server_metadata(args: argparse.Namespace) -> dict:
    health = _http_get(
        args.host, args.port, "/healthz", args.api_key, args.timeout, args.https
    )
    if not health.strip():
        raise AssertionError("Qdrant /healthz returned an empty response")

    raw_info = _http_get(
        args.host, args.port, "/", args.api_key, args.timeout, args.https
    )
    info = json.loads(raw_info)
    actual_version = info.get("version")
    if actual_version != args.expected_server_version:
        raise AssertionError(
            f"expected Qdrant Server {args.expected_server_version}, got {actual_version!r}"
        )
    return info


def _wait_until_deleted(client: QdrantClient, collection: str, point_ids: list[int]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = client.retrieve(
            collection_name=collection,
            ids=point_ids,
            with_payload=False,
            with_vectors=False,
        )
        if not remaining:
            return
        time.sleep(0.1)
    raise AssertionError("delete_point did not remove canonical and alias points")


def _wait_for_server(args: argparse.Namespace) -> dict:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _server_metadata(args)
        except (OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError("Qdrant did not become reachable") from last_error


def validate_qdrant(args: argparse.Namespace | None = None) -> dict:
    args = args or _arguments()
    client_version = importlib.metadata.version("qdrant-client")
    if client_version != EXPECTED_CLIENT_VERSION:
        raise AssertionError(
            f"validator requires qdrant-client {EXPECTED_CLIENT_VERSION}, got {client_version}"
        )

    server_info = _wait_for_server(args)
    collection = f"{APPLICATION_COLLECTION}_compat_{uuid.uuid4().hex}"
    point_id = 424_242
    captured_warnings: list[str] = []
    client: QdrantClient | None = None

    # Production security requires an API key, but the current provider
    # constructor has no api_key argument. Prove that gap against the secured
    # candidate instead of silently treating an unauthenticated server as the
    # production contract. The remaining method-surface test injects the same
    # SDK client configured with the test API key.
    application_providers.SentenceTransformer = lambda _name: _DeterministicModel()
    unconfigured_provider = QdrantProvider(
        host=args.host,
        port=args.port,
        collection_name=collection,
        model_name="deterministic-validation-model",
    )
    try:
        unconfigured_provider.healthcheck()
    except Exception as exc:
        auth_error = str(exc).lower()
        if not any(
            marker in auth_error
            for marker in ("401", "403", "unauthorized", "forbidden")
        ):
            raise AssertionError(
                "Current QdrantProvider failed for a reason other than missing API-key wiring"
            ) from exc
    else:
        raise AssertionError(
            "Secured Qdrant unexpectedly accepted the current provider without an API key"
        )
    finally:
        unconfigured_provider.client.close()

    try:
        with warnings.catch_warnings(record=True) as warning_records:
            warnings.simplefilter("always")
            client = QdrantClient(
                host=args.host,
                port=args.port,
                api_key=args.api_key,
                https=args.https,
                timeout=args.timeout,
            )

            # Establish that the transient target did not exist before the test.
            before = {item.name for item in client.get_collections().collections}
            if collection in before:
                raise AssertionError("generated transient collection unexpectedly exists")

            # Use the application provider itself, replacing only the heavyweight
            # SentenceTransformer model with a deterministic protocol-compatible fake.
            provider = QdrantProvider.__new__(QdrantProvider)
            provider.client = client
            provider.collection_name = collection
            provider.model = _DeterministicModel()

            provider.healthcheck()  # get_collections through application code
            provider.ensure_collection()
            provider.ensure_collection()  # idempotency / existing-collection branch

            after = {item.name for item in client.get_collections().collections}
            if collection not in after:
                raise AssertionError("ensure_collection did not create the test collection")

            details = client.get_collection(collection)
            vectors = details.config.params.vectors
            if getattr(vectors, "size", None) != VECTOR_SIZE:
                raise AssertionError("collection vector dimension differs from model dimension")
            if getattr(vectors, "distance", None) != Distance.COSINE:
                raise AssertionError("application collection distance is not cosine")

            question = "AUZEF uyumluluk kanonik soru"
            answer = "AUZEF uyumluluk cevabı"
            alias = "AUZEF uyumluluk alias sorgusu"
            provider.upsert_point(point_id, question, answer, queries=[alias])

            alias_id = provider._alias_point_id(point_id, 1)
            stored_alias = client.retrieve(
                collection_name=collection,
                ids=[alias_id],
                with_payload=True,
                with_vectors=True,
            )
            if len(stored_alias) != 1:
                raise AssertionError("alias point was not stored")
            if stored_alias[0].payload.get("matched_query") != alias:
                raise AssertionError("alias payload contract was not preserved")
            if stored_alias[0].payload.get("qna_id") != point_id:
                raise AssertionError("alias qna_id payload contract was not preserved")

            # Exercise query_points through both the raw SDK and application search.
            raw_results = client.query_points(
                collection_name=collection,
                query=provider.model.encode(alias).tolist(),
                limit=2,
                with_payload=True,
            )
            if not raw_results.points:
                raise AssertionError("raw query_points returned no results")
            app_results = provider.search(alias, limit=2)
            if not app_results:
                raise AssertionError("QdrantProvider.search returned no results")
            if not any(result.get("matched_query") == alias for result in app_results):
                raise AssertionError("application search did not return the alias match")

            provider.delete_point(point_id)
            _wait_until_deleted(client, collection, [point_id, alias_id])

            captured_warnings = [str(item.message) for item in warning_records]
    finally:
        if client is not None:
            # Also handle a partial create where the SDK raised after the server
            # accepted the mutation. The UUID target makes this lookup safe.
            existing = {item.name for item in client.get_collections().collections}
            if collection in existing:
                client.delete_collection(collection_name=collection)
            remaining = {item.name for item in client.get_collections().collections}
            if collection in remaining:
                raise AssertionError("transient collection cleanup failed")
            client.close()

    result = {
        "status": "compatible",
        "client_version": client_version,
        "server_version": server_info["version"],
        "application_collection": APPLICATION_COLLECTION,
        "transient_collection": collection,
        "vector_dimension": VECTOR_SIZE,
        "vector_dimension_source": "provider.model.get_sentence_embedding_dimension",
        "distance": "cosine",
        "security_blocker": "current QdrantProvider constructor has no API-key wiring",
        "operations": [
            "healthz",
            "version",
            "secured_constructor_rejects_missing_api_key",
            "get_collections",
            "ensure_collection",
            "create_collection",
            "upsert_point",
            "alias_payload",
            "query_points",
            "provider_search",
            "get_collection",
            "delete_point",
            "collection_cleanup",
        ],
        "warnings": captured_warnings,
    }
    return result


def main() -> int:
    print(json.dumps(validate_qdrant(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

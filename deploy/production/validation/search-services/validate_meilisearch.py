#!/usr/bin/env python3
"""Validate the production Meilisearch candidate against the application SDK.

This validator is intentionally destructive only to a randomly named transient
index.  It never opens, mutates, or deletes the application's
``auzef_qna_index`` index.

Environment:
    MEILI_URL             Candidate server URL (required).
    MEILI_MASTER_KEY      Master key configured on the candidate (required).
    EXPECTED_MEILI_VERSION Expected server version (defaults to 1.53.2).
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
import uuid
import warnings
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# providers.py imports SentenceTransformer at module load. Compatibility tests
# must not download model weights; MeiliSearchProvider never uses this class.
sentence_transformers_stub = types.ModuleType("sentence_transformers")
sentence_transformers_stub.SentenceTransformer = object
sys.modules.setdefault("sentence_transformers", sentence_transformers_stub)

import meilisearch  # noqa: E402
from services.providers import MeiliSearchProvider  # noqa: E402


EXPECTED_VERSION = os.getenv("MEILI_EXPECTED_VERSION", "1.53.2")
EXPECTED_CLIENT_VERSION = "0.43.0"
TASK_TIMEOUT_SECONDS = 30


def _task_uid(task: Any) -> int:
    """Accept SDK task objects returned as either dicts or typed wrappers."""
    if isinstance(task, dict):
        value = task.get("taskUid", task.get("uid"))
    else:
        value = getattr(task, "task_uid", getattr(task, "uid", None))
    if value is None:
        raise AssertionError(f"Meilisearch SDK returned no task UID: {task!r}")
    return int(value)


def _task_status(task: Any) -> str | None:
    if isinstance(task, dict):
        return task.get("status")
    return getattr(task, "status", None)


def _wait_for_task(client: Any, task: Any) -> Any:
    completed = client.wait_for_task(
        _task_uid(task), timeout_in_ms=TASK_TIMEOUT_SECONDS * 1000
    )
    status = _task_status(completed)
    if status != "succeeded":
        raise AssertionError(
            f"Meilisearch task {_task_uid(task)} completed with status {status!r}"
        )
    return completed


def _server_version(client: Any) -> str:
    version = client.get_version()
    if not isinstance(version, dict):
        raise AssertionError(f"Unexpected Meilisearch version response: {version!r}")
    reported = version.get("pkgVersion") or version.get("pkg_version")
    if reported != EXPECTED_VERSION:
        raise AssertionError(
            f"Expected Meilisearch Server {EXPECTED_VERSION}, got {reported!r}"
        )
    return str(reported)


def _wait_until_missing(index: Any, document_id: int) -> None:
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            index.get_document(document_id)
        except Exception:  # The SDK maps a not-found response to an exception.
            return
        time.sleep(0.1)
    raise AssertionError(f"Document {document_id} was not deleted in time")


def _wait_for_server(provider: MeiliSearchProvider) -> None:
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            provider.healthcheck()
            return
        except Exception as exc:  # Service startup is asynchronous.
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError("Meilisearch did not become healthy") from last_error


def validate_meilisearch() -> dict[str, Any]:
    url = os.getenv("MEILI_URL", "").strip()
    master_key = os.getenv("MEILI_MASTER_KEY", "").strip()
    if not url:
        raise SystemExit("MEILI_URL is required")
    if not master_key:
        raise SystemExit(
            "MEILI_MASTER_KEY is required; unauthenticated validation is forbidden"
        )

    index_name = f"auzef_compat_{uuid.uuid4().hex}"
    installed_client_version = distribution_version("meilisearch")
    if installed_client_version != EXPECTED_CLIENT_VERSION:
        raise AssertionError(
            f"Expected meilisearch SDK {EXPECTED_CLIENT_VERSION}, "
            f"got {installed_client_version}"
        )
    provider = MeiliSearchProvider(
        url=url,
        master_key=master_key,
        index_name=index_name,
    )
    client = provider.client
    created = False
    steps: list[str] = []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            _wait_for_server(provider)
            steps.append("health")

            server_version = _server_version(client)
            steps.append("server_version")

            try:
                meilisearch.Client(url).get_indexes()
            except Exception:
                steps.append("unauthenticated_access_rejected")
            else:
                raise AssertionError(
                    "Candidate server allowed index access without the master key"
                )

            create_task = client.create_index(index_name, {"primaryKey": "id"})
            created = True
            _wait_for_task(client, create_task)
            index = client.get_index(index_name)
            if index.uid != index_name:
                raise AssertionError("Created index could not be accessed by UID")
            steps.extend(["index_create", "index_access"])

            # importer.py configures precisely these application fields.  No
            # filterable/sortable attributes are configured by current code.
            _wait_for_task(
                client,
                index.update_searchable_attributes(
                    ["question", "queries", "tags", "answer"]
                ),
            )
            settings = index.get_settings()
            if settings.get("searchableAttributes") != [
                "question",
                "queries",
                "tags",
                "answer",
            ]:
                raise AssertionError("searchableAttributes contract was not applied")
            steps.append("searchable_attributes")

            documents = [
                {
                    "id": 101,
                    "question": "Kayıt yenileme nasıl yapılır?",
                    "answer": "AKSİS üzerinden yapılır.",
                    "queries": ["kayıt işlemleri", "ders kaydı"],
                    "tags": ["kayıt", "AKSİS"],
                },
                {
                    "id": 202,
                    "question": "Sınav sonucu nereden görülür?",
                    "answer": "Sonuç ekranından görülür.",
                    "queries": ["notumu görmek istiyorum"],
                    "tags": ["sınav"],
                },
            ]
            # This is the exact SDK index call wrapped by
            # MeiliSearchProvider.add_documents; retaining the task object is
            # necessary to prove asynchronous completion.
            _wait_for_task(client, index.add_documents(documents))
            steps.extend(["add_documents", "task_wait"])

            hits = provider.search("kayıt yenileme", limit=3)
            if not hits or hits[0]["qna_id"] != 101:
                raise AssertionError("Provider search did not return the expected document")
            if not isinstance(hits[0]["score"], (int, float)):
                raise AssertionError("showRankingScore did not return _rankingScore")
            steps.extend(["provider_search", "show_ranking_score"])

            suggestions = provider.get_suggestions("kayıt işlemleri alakasız", limit=3)
            if "Kayıt yenileme nasıl yapılır?" not in suggestions:
                raise AssertionError("matchingStrategy=last suggestion search failed")
            steps.append("matching_strategy_last")

            _wait_for_task(
                client,
                index.update_documents(
                    [{"id": 101, "answer": "Güncel yanıt AKSİS'tedir."}]
                ),
            )
            updated = index.get_document(101)
            updated_answer = (
                updated.get("answer")
                if isinstance(updated, dict)
                else getattr(updated, "answer", None)
            )
            if updated_answer != "Güncel yanıt AKSİS'tedir.":
                raise AssertionError("update_documents did not update the document")
            steps.append("update_documents")

            _wait_for_task(client, index.delete_document(202))
            _wait_until_missing(index, 202)
            steps.append("delete_document")
        finally:
            if created:
                _wait_for_task(client, client.delete_index(index_name))
                steps.append("index_cleanup")

    warning_messages = [str(item.message) for item in caught]
    return {
        "status": "compatible",
        "client": f"meilisearch=={installed_client_version}",
        "server": server_version,
        "transient_index": index_name,
        "steps": steps,
        "warnings": warning_messages,
    }


def main() -> int:
    try:
        result = validate_meilisearch()
    except Exception as exc:
        print(
            json.dumps(
                {"status": "incompatible", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

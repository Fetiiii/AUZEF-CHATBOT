"""Run both real search-service compatibility validators."""

import json

from validate_meilisearch import validate_meilisearch
from validate_qdrant import validate_qdrant


def main() -> None:
    meili_result = validate_meilisearch()
    qdrant_result = validate_qdrant()
    print(
        json.dumps(
            {
                "status": "compatible",
                "meilisearch": meili_result,
                "qdrant": qdrant_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

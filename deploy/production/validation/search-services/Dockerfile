FROM python:3.11.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

# Install only the two production client SDKs and their dependencies, while
# constraining every resolved package to the committed production graph.
COPY backend/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir \
        --constraint /tmp/requirements.lock \
        qdrant-client==1.19.0 \
        meilisearch==0.43.0 \
    && python -m pip check

COPY backend/services /workspace/backend/services
COPY deploy/production/validation/search-services \
    /workspace/deploy/production/validation/search-services

ENV PYTHONPATH=/workspace/backend

CMD ["python", "/workspace/deploy/production/validation/search-services/run_compatibility.py"]

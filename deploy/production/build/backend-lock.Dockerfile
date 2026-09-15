# Build-only image used by refresh-backend-lock.sh. It is never deployed.
FROM python:3.11-slim AS cpu-base

ENV CUDA_VISIBLE_DEVICES="" \
    NVIDIA_VISIBLE_DEVICES=void \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1 is the OpenMP runtime used by the CPU numerical stack. Package
# installation on production VMs remains outside this build-only contract.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace


# Resolve the human-managed direct requirements only after installing the
# existing validated torch version from PyTorch's official CPU wheel index.
FROM cpu-base AS resolver

ARG TORCH_CPU_VERSION=2.14.0+cpu
ARG PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu

COPY requirements.txt ./requirements.txt

RUN python -m pip install --no-cache-dir --timeout 120 --retries 10 \
        "torch==${TORCH_CPU_VERSION}" --index-url "${PYTORCH_CPU_INDEX}" \
    && python -m pip install --no-cache-dir --timeout 120 --retries 10 \
        -r requirements.txt \
    && python -m pip check \
    && python -c "import sentence_transformers, torch; assert torch.__version__ == '${TORCH_CPU_VERSION}'; assert torch.cuda.is_available() is False"


# Independently prove that the committed production command can reconstruct
# the graph without dependency resolution. The lock itself carries the
# official CPU index directive needed to locate torch's +cpu distribution.
FROM cpu-base AS validation

ARG LOCK_SOURCE=requirements.lock
COPY ${LOCK_SOURCE} ./requirements.lock

RUN python -m pip install --no-cache-dir --timeout 120 --retries 10 \
        --no-deps -r requirements.lock \
    && python -m pip check \
    && python -c "import sentence_transformers, torch; assert torch.__version__ == '2.14.0+cpu'; assert torch.cuda.is_available() is False"

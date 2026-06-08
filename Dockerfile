# Use the bundled Dockerfile frontend so builds do not depend on fetching
# docker/dockerfile:1 from Docker Hub at build time.
# Initialize device type args
# use build args in the docker build command with --build-arg="BUILDARG=true"
ARG USE_CUDA=false
ARG USE_OLLAMA=false
ARG USE_SLIM=false
ARG USE_PERMISSION_HARDENING=false
# Tested with cu117 for CUDA 11 and cu121 for CUDA 12 (default)
ARG USE_CUDA_VER=cu128
# any sentence transformer model; models to use can be found at https://huggingface.co/models?library=sentence-transformers
# Leaderboard: https://huggingface.co/spaces/mteb/leaderboard 
# for better performance and multilangauge support use "intfloat/multilingual-e5-large" (~2.5GB) or "intfloat/multilingual-e5-base" (~1.5GB)
# IMPORTANT: If you change the embedding model (sentence-transformers/all-MiniLM-L6-v2) and vice versa, you aren't able to use RAG Chat with your previous documents loaded in the WebUI! You need to re-embed them.
ARG USE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ARG USE_RERANKING_MODEL=""
ARG USE_AUXILIARY_EMBEDDING_MODEL=TaylorAI/bge-micro-v2

# Tiktoken encoding name; models to use can be found at https://huggingface.co/models?library=tiktoken
ARG USE_TIKTOKEN_ENCODING_NAME="cl100k_base"

ARG BUILD_HASH=dev-build
# Override at your own risk - non-root configurations are untested
ARG UID=0
ARG GID=0
ARG NODE_IMAGE=node:22-alpine3.20
ARG PYTHON_IMAGE=python:3.11.14-slim-bookworm
ARG NPM_CONFIG_REGISTRY
ARG GITHUB_MIRROR_PREFIX
ARG NODE_MAX_OLD_SPACE_SIZE=4096
ARG SKIP_PYODIDE_FETCH=false
ARG SKIP_NLTK_PRELOAD=false
ARG HF_ENDPOINT=https://huggingface.co
ARG PIP_INDEX_URL
ARG UV_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PYTORCH_INDEX_URL_CPU
ARG PYTORCH_INDEX_URL_CUDA
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
ARG SKIP_NLTK_PRELOAD

######## WebUI frontend ########
FROM ${NODE_IMAGE} AS build
ARG NPM_CONFIG_REGISTRY
ARG GITHUB_MIRROR_PREFIX
ARG NODE_MAX_OLD_SPACE_SIZE
ARG SKIP_PYODIDE_FETCH
ARG ONNXRUNTIME_NODE_INSTALL_CUDA=skip

ENV NODE_MAX_OLD_SPACE_SIZE=${NODE_MAX_OLD_SPACE_SIZE}

WORKDIR /app

ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
ENV GITHUB_MIRROR_PREFIX=${GITHUB_MIRROR_PREFIX}
ENV ONNXRUNTIME_NODE_INSTALL_CUDA=${ONNXRUNTIME_NODE_INSTALL_CUDA}

COPY open-webui/scripts/github-mirror.js /app/scripts/github-mirror.js
COPY open-webui/package.json open-webui/package-lock.json ./
RUN if [ -n "$NPM_CONFIG_REGISTRY" ]; then npm config set registry "$NPM_CONFIG_REGISTRY"; fi
ENV NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE_SIZE} --require /app/scripts/github-mirror.js"
RUN npm ci --force

# Copy only the frontend inputs so backend-only changes keep the prebaked
# node_modules layer and frontend build cache intact.
COPY open-webui/CHANGELOG.md ./CHANGELOG.md
COPY open-webui/postcss.config.js open-webui/svelte.config.js open-webui/tailwind.config.js open-webui/tsconfig.json open-webui/vite.config.ts ./
COPY open-webui/src ./src
COPY open-webui/static ./static
COPY open-webui/scripts ./scripts
# Keep the deploy/version hash late so changing it does not invalidate the
# cached frontend dependency install.
ARG BUILD_HASH
ENV APP_BUILD_HASH=${BUILD_HASH}
RUN if [ "$SKIP_PYODIDE_FETCH" = "true" ]; then \
    echo "Skipping pyodide fetch for faster build"; \
    npx vite build; \
  else \
    npm run build; \
  fi

######## WebUI backend ########
FROM ${PYTHON_IMAGE} AS base

# Use args
ARG USE_CUDA
ARG USE_OLLAMA
ARG USE_CUDA_VER
ARG USE_SLIM
ARG USE_PERMISSION_HARDENING
ARG USE_EMBEDDING_MODEL
ARG USE_RERANKING_MODEL
ARG USE_AUXILIARY_EMBEDDING_MODEL
ARG UID
ARG GID
ARG HF_ENDPOINT=https://huggingface.co
ARG PIP_INDEX_URL
ARG UV_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PYTORCH_INDEX_URL_CPU
ARG PYTORCH_INDEX_URL_CUDA
ARG GITHUB_MIRROR_PREFIX
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
ARG SKIP_NLTK_PRELOAD

# Python settings
ENV PYTHONUNBUFFERED=1

## Basis ##
ENV ENV=prod \
    PORT=8080 \
    # pass build args to the build
    USE_OLLAMA_DOCKER=${USE_OLLAMA} \
    USE_CUDA_DOCKER=${USE_CUDA} \
    USE_SLIM_DOCKER=${USE_SLIM} \
    USE_CUDA_DOCKER_VER=${USE_CUDA_VER} \
    USE_EMBEDDING_MODEL_DOCKER=${USE_EMBEDDING_MODEL} \
    USE_RERANKING_MODEL_DOCKER=${USE_RERANKING_MODEL} \
    USE_AUXILIARY_EMBEDDING_MODEL_DOCKER=${USE_AUXILIARY_EMBEDDING_MODEL}

## Basis URL Config ##
ENV OLLAMA_BASE_URL="/ollama" \
    OPENAI_API_BASE_URL=""

## API Key and Security Config ##
ENV OPENAI_API_KEY="" \
    WEBUI_SECRET_KEY="" \
    SCARF_NO_ANALYTICS=true \
    DO_NOT_TRACK=true \
    ANONYMIZED_TELEMETRY=false

## Python package mirrors ##
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_INDEX_URL=${UV_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PYTORCH_INDEX_URL_CPU=${PYTORCH_INDEX_URL_CPU} \
    PYTORCH_INDEX_URL_CUDA=${PYTORCH_INDEX_URL_CUDA}

ENV SKIP_NLTK_PRELOAD=${SKIP_NLTK_PRELOAD}

#### Other models #########################################################
## whisper TTS model settings ##
ENV WHISPER_MODEL="base" \
    WHISPER_MODEL_DIR="/app/backend/data/cache/whisper/models"

## RAG Embedding model settings ##
ENV RAG_EMBEDDING_MODEL="$USE_EMBEDDING_MODEL_DOCKER" \
    RAG_RERANKING_MODEL="$USE_RERANKING_MODEL_DOCKER" \
    AUXILIARY_EMBEDDING_MODEL="$USE_AUXILIARY_EMBEDDING_MODEL_DOCKER" \
    SENTENCE_TRANSFORMERS_HOME="/app/backend/data/cache/embedding/models"

## Tiktoken model settings ##
ENV TIKTOKEN_ENCODING_NAME="cl100k_base" \
    TIKTOKEN_CACHE_DIR="/app/backend/data/cache/tiktoken"

## Hugging Face download cache ##
ENV HF_HOME="/app/backend/data/cache/embedding/models"
ENV HF_ENDPOINT=${HF_ENDPOINT}

## NLTK settings for deterministic unstructured parsing at runtime ##
ENV NLTK_DATA="/usr/local/share/nltk_data" \
    AUTO_DOWNLOAD_NLTK="False"

## Torch Extensions ##
# ENV TORCH_EXTENSIONS_DIR="/.cache/torch_extensions"

#### Other models ##########################################################

WORKDIR /app/backend

ENV HOME=/root
# Create user and group if not root
RUN if [ $UID -ne 0 ]; then \
    if [ $GID -ne 0 ]; then \
    addgroup --gid $GID app; \
    fi; \
    adduser --uid $UID --gid $GID --home $HOME --disabled-password --no-create-home app; \
    fi

RUN mkdir -p $HOME/.cache/chroma
RUN echo -n 00000000-0000-0000-0000-000000000000 > $HOME/.cache/chroma/telemetry_user_id

# Make sure the user has access to the app and root directory
RUN chown -R $UID:$GID /app $HOME

# Install common system dependencies
RUN if [ -n "$APT_MIRROR" ] || [ -n "$APT_SECURITY_MIRROR" ]; then \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    if [ -n "$APT_MIRROR" ]; then \
    sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|https://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -n "$APT_SECURITY_MIRROR" ]; then \
    sed -i "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    fi; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    git build-essential pandoc gcc netcat-openbsd curl jq \
    libmariadb-dev \
    python3-dev \
    ffmpeg libsm6 libxext6 zstd \
    && rm -rf /var/lib/apt/lists/*

# install python dependencies
COPY --chown=$UID:$GID open-webui/backend/requirements.txt ./requirements.txt
COPY --chown=$UID:$GID open-webui/backend/requirements-min.txt ./requirements-min.txt
COPY --chown=$UID:$GID retrieval-engine/pyproject.toml /tmp/retrieval-engine/pyproject.toml
COPY --chown=$UID:$GID retrieval-engine/retrieval_engine /tmp/retrieval-engine/retrieval_engine

RUN set -e; \
    pip3 install --no-cache-dir uv && \
    if [ "$USE_CUDA" = "true" ]; then \
    # If you use CUDA the whisper and embedding model will be downloaded on first use
    # fix: pin torch<=2.9.1 - torch 2.10.0 aarch64 wheels cause SIGILL on ARM devices (RPi 4 Cortex-A72) #21349
    PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL_CUDA:-https://download.pytorch.org/whl/$USE_CUDA_DOCKER_VER}" && \
    pip3 install 'torch<=2.9.1' torchvision torchaudio --index-url "$PYTORCH_INDEX_URL" --trusted-host "$(echo "$PYTORCH_INDEX_URL" | sed -E 's#^https?://([^/]+)/?.*#\1#')" --no-cache-dir && \
    uv pip install --system -r requirements.txt --no-cache-dir && \
    python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['RAG_EMBEDDING_MODEL'], device='cpu')" && \
    python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ.get('AUXILIARY_EMBEDDING_MODEL', 'TaylorAI/bge-micro-v2'), device='cpu')" && \
    python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], device='cpu', compute_type='int8', download_root=os.environ['WHISPER_MODEL_DIR'])"; \
    python -c "import os; import tiktoken; tiktoken.get_encoding(os.environ['TIKTOKEN_ENCODING_NAME'])"; \
    python -c "import nltk; nltk.download('punkt_tab')"; \
    else \
    PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL_CPU:-https://download.pytorch.org/whl/cpu}" && \
    pip3 install 'torch<=2.9.1' torchvision torchaudio --index-url "$PYTORCH_INDEX_URL" --trusted-host "$(echo "$PYTORCH_INDEX_URL" | sed -E 's#^https?://([^/]+)/?.*#\1#')" --no-cache-dir && \
    uv pip install --system -r requirements.txt --no-cache-dir && \
    if [ "$USE_SLIM" != "true" ]; then \
    python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['RAG_EMBEDDING_MODEL'], device='cpu')" && \
    python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ.get('AUXILIARY_EMBEDDING_MODEL', 'TaylorAI/bge-micro-v2'), device='cpu')" && \
    python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], device='cpu', compute_type='int8', download_root=os.environ['WHISPER_MODEL_DIR'])"; \
    python -c "import os; import tiktoken; tiktoken.get_encoding(os.environ['TIKTOKEN_ENCODING_NAME'])"; \
    python -c "import nltk; nltk.download('punkt_tab')"; \
    fi; \
    fi; \
    pip3 install --no-cache-dir -r requirements-min.txt && \
    pip3 install --no-cache-dir psycopg2-binary==2.9.11 pgvector==0.4.2 && \
    mkdir -p /app/backend/data && chown -R $UID:$GID /app/backend/data/ && \
    rm -rf /var/lib/apt/lists/*;

# Install the standalone retrieval-engine package into the runtime image so
# Open WebUI can import it without relying on sibling source paths or mounts.
RUN pip3 install --no-cache-dir /tmp/retrieval-engine

# S3-backed deployments require boto3 at runtime even when the slim/min install
# path skips the full backend requirements layer.
RUN pip3 install --no-cache-dir boto3==1.42.62

# Ensure the local STT stack is always present in the runtime image.
# Slim/cached builds have intermittently booted without faster-whisper even
# though local audio transcription is enabled by default.
RUN pip3 install --no-cache-dir faster-whisper==1.2.1 modelscope==1.36.0

# Ensure the local embedding stack is always present in the runtime image.
# Slim/cached builds can otherwise boot without the packages needed to index
# file content for retrieval, even when the embedding model cache already exists.
RUN set -e; \
    PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL_CPU:-https://download.pytorch.org/whl/cpu}" && \
    pip3 install --no-cache-dir \
        'torch<=2.9.1' \
        --index-url "$PYTORCH_INDEX_URL" \
        --trusted-host "$(echo "$PYTORCH_INDEX_URL" | sed -E 's#^https?://([^/]+)/?.*#\1#')" && \
    pip3 install --no-cache-dir \
        transformers==5.3.0 \
        sentence-transformers==5.2.3 \
        accelerate

# Ensure the document extraction stack is always present in the runtime image.
# This guards against slim/cached builds shipping without the packages needed
# to read uploaded office documents during chat.
RUN pip3 install --no-cache-dir \
    unstructured==0.18.31 \
    pandas==3.0.1 \
    msoffcrypto-tool==6.0.0 \
    networkx==3.4.2 \
    openpyxl==3.1.5 \
    pyxlsb==1.0.10 \
    xlrd==2.0.2 \
    docx2txt==0.9 \
    python-pptx==1.0.2 \
    pypandoc==1.16.2 \
    nltk==3.9.3

# Fail the build if the runtime image still lacks core backend or file
# extraction packages.
RUN python3 - <<'PY'
import importlib.util

required_modules = {
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "psycopg2": "psycopg2-binary",
    "uvicorn": "uvicorn",
    "typer": "typer",
    "docx2txt": "docx2txt",
    "msoffcrypto": "msoffcrypto-tool",
    "networkx": "networkx",
    "nltk": "nltk",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "pptx": "python-pptx",
    "pypandoc": "pypandoc",
    "pyxlsb": "pyxlsb",
    "sentence_transformers": "sentence-transformers",
    "torch": "torch",
    "transformers": "transformers",
    "accelerate": "accelerate",
    "ctranslate2": "ctranslate2",
    "faster_whisper": "faster-whisper",
    "modelscope": "modelscope",
    "retrieval_engine": "retrieval-engine",
    "unstructured": "unstructured",
    "xlrd": "xlrd",
}
missing = sorted(
    package_name
    for module_name, package_name in required_modules.items()
    if importlib.util.find_spec(module_name) is None
)

if missing:
    raise RuntimeError(
        "Missing core runtime packages after dependency install: "
        + ", ".join(missing)
    )

print("Dependency check OK: core runtime packages present.")
PY

# Preload NLTK resources required by unstructured Excel/document loaders.
# Keep the preload logic in a real Python module so classic Docker builds do
# not silently keep a stale heredoc script when only the logic changes.
COPY --chown=$UID:$GID open-webui/backend/open_webui/utils/nltk_preload.py /tmp/nltk_preload.py
RUN mkdir -p "$NLTK_DATA" && \
    printf 'nltk-preload-v7\n' > "$NLTK_DATA/.image-marker" && \
    python3 /tmp/nltk_preload.py --build-preload

# Install Ollama if requested
RUN if [ "$USE_OLLAMA" = "true" ]; then \
    date +%s > /tmp/ollama_build_hash && \
    echo "Cache broken at timestamp: `cat /tmp/ollama_build_hash`" && \
    curl -fsSL https://ollama.com/install.sh | sh && \
    rm -rf /var/lib/apt/lists/*; \
    fi

# copy embedding weight from build
# RUN mkdir -p /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2
# COPY --from=build /app/onnx /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx

# copy built frontend files
COPY --chown=$UID:$GID --from=build /app/build /app/build
COPY --chown=$UID:$GID --from=build /app/CHANGELOG.md /app/CHANGELOG.md
COPY --chown=$UID:$GID --from=build /app/package.json /app/package.json

# copy backend files
COPY --chown=$UID:$GID open-webui/backend .

# Fail the build if the packaged retrieval engine or its Open WebUI adapter
# still cannot be imported from the final runtime filesystem.
RUN python3 - <<'PY'
import retrieval_engine
import open_webui.retrieval.engine_adapter

print(f"Retrieval engine import OK: {retrieval_engine.__file__}")
PY

# Keep the runtime API's build hash aligned with the frontend assets without
# invalidating the heavy dependency layers when the deploy tag changes.
ARG BUILD_HASH
ENV WEBUI_BUILD_HASH=${BUILD_HASH}

# Runtime-only mirror configuration is kept late so changing the mirror does not
# invalidate the prebaked apt/pip dependency layers above.
ENV GITHUB_MIRROR_PREFIX=${GITHUB_MIRROR_PREFIX}

EXPOSE 8080

HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1

# Minimal, atomic permission hardening for OpenShift (arbitrary UID):
# - Group 0 owns /app and /root
# - Directories are group-writable and have SGID so new files inherit GID 0
RUN if [ "$USE_PERMISSION_HARDENING" = "true" ]; then \
    set -eux; \
    chgrp -R 0 /app /root || true; \
    chmod -R g+rwX /app /root || true; \
    find /app -type d -exec chmod g+s {} + || true; \
    find /root -type d -exec chmod g+s {} + || true; \
    fi

USER $UID:$GID

ARG BUILD_HASH
ENV WEBUI_BUILD_VERSION=${BUILD_HASH}
ENV DOCKER=true

CMD [ "bash", "start.sh"]

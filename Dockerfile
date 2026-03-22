# syntax=docker/dockerfile:1
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
ARG HF_ENDPOINT
ARG PIP_INDEX_URL
ARG UV_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PYTORCH_INDEX_URL_CPU
ARG PYTORCH_INDEX_URL_CUDA
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR

######## WebUI frontend ########
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
ARG BUILD_HASH
ARG NPM_CONFIG_REGISTRY
ARG GITHUB_MIRROR_PREFIX
ARG NODE_MAX_OLD_SPACE_SIZE
ARG SKIP_PYODIDE_FETCH

ENV NODE_MAX_OLD_SPACE_SIZE=${NODE_MAX_OLD_SPACE_SIZE}

WORKDIR /app

# to store git revision in build
RUN apk add --no-cache git

ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
ENV GITHUB_MIRROR_PREFIX=${GITHUB_MIRROR_PREFIX}

COPY scripts/github-mirror.js /app/scripts/github-mirror.js
COPY package.json package-lock.json ./
RUN if [ -n "$NPM_CONFIG_REGISTRY" ]; then npm config set registry "$NPM_CONFIG_REGISTRY"; fi
ENV NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE_SIZE} --require /app/scripts/github-mirror.js"
RUN npm ci --force

COPY . .
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
ARG HF_ENDPOINT
ARG PIP_INDEX_URL
ARG UV_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PYTORCH_INDEX_URL_CPU
ARG PYTORCH_INDEX_URL_CUDA
ARG GITHUB_MIRROR_PREFIX
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR

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
COPY --chown=$UID:$GID ./backend/requirements.txt ./requirements.txt
COPY --chown=$UID:$GID ./backend/requirements-min.txt ./requirements-min.txt

RUN pip3 install --no-cache-dir uv && \
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

# Fail the build if the runtime image still lacks core backend packages.
RUN python3 - <<'PY'
import importlib.util

required_modules = {
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "psycopg2": "psycopg2-binary",
    "uvicorn": "uvicorn",
    "typer": "typer",
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
# We use mirror-first direct package URLs (if provided), then fall back to upstream.
RUN python3 - <<'PY'
import io
import os
import urllib.request
import zipfile

download_dir = os.environ.get("NLTK_DATA", "/usr/local/share/nltk_data")
mirror_prefix = (os.environ.get("GITHUB_MIRROR_PREFIX") or "").strip().rstrip("/")

resources = [
    ("taggers", "averaged_perceptron_tagger_eng"),
    ("tokenizers", "punkt_tab"),
]

os.makedirs(download_dir, exist_ok=True)

def candidates(category: str, package: str):
    paths = []
    if mirror_prefix:
        paths.append(
            f"{mirror_prefix}/nltk/nltk_data/raw/gh-pages/packages/{category}/{package}.zip"
        )
    paths.append(
        f"https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{category}/{package}.zip"
    )
    return paths

def present(category: str, package: str) -> bool:
    return os.path.isdir(os.path.join(download_dir, category, package))

def normalize(category: str, package: str):
    # Handle previously flattened layout: /nltk_data/<package> -> /nltk_data/<category>/<package>
    flat = os.path.join(download_dir, package)
    nested = os.path.join(download_dir, category, package)
    if os.path.isdir(flat) and not os.path.isdir(nested):
        os.makedirs(os.path.join(download_dir, category), exist_ok=True)
        os.replace(flat, nested)

def extract_archive(zf: zipfile.ZipFile, category: str, package: str):
    names = [n for n in zf.namelist() if n and not n.startswith("__MACOSX/")]
    category_prefix = f"{category}/"
    package_prefix = f"{package}/"

    if any(n.startswith(category_prefix) for n in names):
        zf.extractall(download_dir)
    elif any(n.startswith(package_prefix) for n in names):
        zf.extractall(os.path.join(download_dir, category))
    else:
        target_root = os.path.join(download_dir, category, package)
        os.makedirs(target_root, exist_ok=True)
        for name in names:
            if name.endswith("/"):
                os.makedirs(os.path.join(target_root, name), exist_ok=True)
                continue
            dest = os.path.join(target_root, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                out.write(src.read())

for category, package in resources:
    normalize(category, package)
    if present(category, package):
        print(f"NLTK resource already present: {category}/{package}")
        continue

    last_error = None
    for url in candidates(category, package):
        try:
            print(f"Downloading {category}/{package} from {url}")
            with urllib.request.urlopen(url, timeout=120) as response:
                archive = response.read()
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                extract_archive(zf, category, package)
            normalize(category, package)
            if present(category, package):
                print(f"Installed {category}/{package} via {url}")
                last_error = None
                break
            last_error = RuntimeError(
                f"Package extracted but not discoverable: {category}/{package}"
            )
        except Exception as exc:  # pragma: no cover - build-time path
            print(f"Download failed from {url}: {exc}")
            last_error = exc

    if last_error is not None:
        raise RuntimeError(
            f"Failed to preload NLTK resource {category}/{package}: {last_error}"
        )

print("NLTK preload complete.")
PY

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
COPY --chown=$UID:$GID ./backend .

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

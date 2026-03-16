import os
import shutil
import tempfile
from pathlib import Path


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="open-webui-local-tests-"))
SOURCE_DB = Path(__file__).resolve().parents[2] / "data" / "webui.db"
TEST_DB = TEST_DATA_DIR / "webui.db"

if SOURCE_DB.exists():
    shutil.copy2(SOURCE_DB, TEST_DB)

os.environ.setdefault("ENV", "test")
os.environ.setdefault("OFFLINE_MODE", "true")
os.environ.setdefault("WEBUI_SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATA_DIR", str(TEST_DATA_DIR))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB}")
os.environ.setdefault("VECTOR_DB", "noop")
os.environ.setdefault("SKIP_PLUGIN_DEPENDENCY_INSTALL", "true")
os.environ.setdefault("ENABLE_OPENAI_API", "false")
os.environ.setdefault("ENABLE_OLLAMA_API", "false")
os.environ.setdefault("ENABLE_BASE_MODELS_CACHE", "false")
os.environ.setdefault("BYPASS_EMBEDDING_AND_RETRIEVAL", "true")

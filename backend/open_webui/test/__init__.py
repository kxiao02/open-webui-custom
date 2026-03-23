import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="open-webui-local-tests-"))
SOURCE_DB = Path(__file__).resolve().parents[2] / "data" / "webui.db"
TEST_DB = TEST_DATA_DIR / "webui.db"

test_db_url = os.environ.get("OPEN_WEBUI_TEST_DATABASE_URL")
if not test_db_url:
    if SOURCE_DB.exists():
        shutil.copy2(SOURCE_DB, TEST_DB)
        with sqlite3.connect(TEST_DB) as conn:
            user_columns = {
                row[1] for row in conn.execute('PRAGMA table_info("user")').fetchall()
            }
            if "scim" not in user_columns:
                conn.execute('ALTER TABLE "user" ADD COLUMN scim JSON')
            conn.commit()
    test_db_url = f"sqlite:///{TEST_DB}"

os.environ.setdefault("ENV", "test")
os.environ.setdefault("OFFLINE_MODE", "true")
os.environ.setdefault("WEBUI_SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATA_DIR", str(TEST_DATA_DIR))
os.environ["DATABASE_URL"] = test_db_url
os.environ.setdefault("VECTOR_DB", "noop")
os.environ.setdefault("SKIP_PLUGIN_DEPENDENCY_INSTALL", "true")
os.environ.setdefault("ENABLE_OPENAI_API", "false")
os.environ.setdefault("ENABLE_OLLAMA_API", "false")
os.environ.setdefault("ENABLE_BASE_MODELS_CACHE", "false")
os.environ.setdefault("BYPASS_EMBEDDING_AND_RETRIEVAL", "true")

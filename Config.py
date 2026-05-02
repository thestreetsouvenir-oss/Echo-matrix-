from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

VAULT_DIR = Path.home() / "echo_matrix"
VAULT_DIR.mkdir(exist_ok=True)
DB_PATH = VAULT_DIR / "echo.db"
ARCHIVE_PATH = VAULT_DIR / "archive.jsonl"
TOOLS_DIR = VAULT_DIR / "tools"
TOOLS_DIR.mkdir(exist_ok=True)
DOCS_DIR = VAULT_DIR / "raw_docs"
DOCS_DIR.mkdir(exist_ok=True)
BOOKS_DIR = VAULT_DIR / "books"
BOOKS_DIR.mkdir(exist_ok=True)

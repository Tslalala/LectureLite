"""Isolated local UI verification. Never opens or changes the user's store."""
import sys
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve

root = Path(tempfile.mkdtemp(prefix="lecturelite-preview-"))
serve.DATA_DIR = root
serve.DB_FILE = root / "store.json"
serve.SHARED_DIR = root / "shared"
serve.SHARED_DIR.mkdir()
serve.URL_SCHEME = "http"
serve._register_user("tutorial", "LectureLite-demo-2026")
serve._seed_demo_courses()
print("Isolated preview: http://127.0.0.1:8771/home.html", flush=True)
ThreadingHTTPServer(("127.0.0.1",8771),serve.Handler).serve_forever()

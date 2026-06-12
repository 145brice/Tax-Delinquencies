import os
import sys
import traceback

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

_flask_app = None
_startup_error = None


def _load_app():
    global _flask_app, _startup_error
    if _flask_app is not None:
        return _flask_app
    try:
        from app import app as loaded_app  # noqa: E402
        _flask_app = loaded_app
        _startup_error = None
        return _flask_app
    except Exception:
        _startup_error = traceback.format_exc()
        return None


def app(environ, start_response):
    if environ.get("PATH_INFO") == "/healthz":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"ok":true,"entrypoint":"api/index.py"}']

    loaded_app = _load_app()
    if loaded_app is None:
        body = (_startup_error or "Unknown startup error").encode("utf-8", errors="replace")
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [body]
    return loaded_app(environ, start_response)

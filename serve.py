"""Production entry point for the single Railway service and volume."""
import os
from waitress import serve
from app import app, _persistent_storage_ready

if __name__ == "__main__":
    if not _persistent_storage_ready():
        raise RuntimeError("Railway requires a mounted volume; SQLITE_DB must be inside it")
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8095")), threads=8)

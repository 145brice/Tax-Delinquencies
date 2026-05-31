"""Long-running scraper worker entrypoint.

Deploy this on Render/Railway/Fly/a VPS. Vercel should point
SCRAPER_WORKER_URL at the deployed worker URL.
"""
import os

from app import app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8095"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

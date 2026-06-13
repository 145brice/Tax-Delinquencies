import os

from flask import Flask, redirect, request


RAILWAY_URL = os.getenv(
    "CANONICAL_APP_URL",
    "https://tax-delinquencies-production.up.railway.app",
).rstrip("/")

app = Flask(__name__)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def redirect_to_railway(path):
    query = request.query_string.decode("utf-8")
    target = f"{RAILWAY_URL}/{path}" if path else f"{RAILWAY_URL}/"
    if query:
        target = f"{target}?{query}"
    return redirect(target, code=308)

import logging
import os
from threading import Thread

from flask import Flask, jsonify

app = Flask(__name__)
_status_provider = None


@app.route("/")
def home():
    return "Бот работает!"


@app.route("/health")
def health():
    """JSON с реальным состоянием ботов: 200 — всё в порядке, 503 — нет."""
    if _status_provider is None:
        return jsonify({"status": "starting"}), 503
    status = _status_provider()
    code = 200 if status.get("status") == "ok" else 503
    return jsonify(status), code


def run(port):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=port)


def keep_alive(status_provider=None, port=None):
    global _status_provider
    _status_provider = status_provider
    port = port or int(os.environ.get("PORT", 8080))
    # daemon=True — поток не мешает процессу завершиться по Ctrl+C
    t = Thread(target=run, args=(port,), daemon=True)
    t.start()

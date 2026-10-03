"""Media Studio -- web app entry point.   Run:  python app.py"""
import logging

from flask import Flask

import config
from core.jobs import JobManager, cleanup_old_files
from web.routes import bp


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
    app.config["JOBS"] = JobManager()
    app.register_blueprint(bp)
    cleanup_old_files()
    return app


if __name__ == "__main__":
    app = create_app()
    print(f"\n  Media Studio running at  http://{config.HOST}:{config.PORT}\n  (Ctrl+C to stop)\n")
    app.run(host=config.HOST, port=config.PORT, threaded=True)

import os

from fastapi import FastAPI

IEDUCAR_URL = os.getenv("IEDUCAR_URL", "http://localhost")
MOODLE_URL = os.getenv("MOODLE_URL", "http://localhost:8080")

app = FastAPI(title="i-Educar Moodle Integration")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "ieducar_url": IEDUCAR_URL, "moodle_url": MOODLE_URL}

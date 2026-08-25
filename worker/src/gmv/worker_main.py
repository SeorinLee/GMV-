"""Worker entrypoint: `python -m gmv.worker_main` (spec §8).

Runs the FastAPI app under uvicorn with the REAL Playwright session factory. Must run on a
host that has Google Chrome + Microsoft Edge installed (spec §8) — never on Vercel.
"""

from __future__ import annotations

import os

import uvicorn

from gmv.api import create_app

app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("GMV_HOST", "127.0.0.1"),
        port=int(os.environ.get("GMV_PORT", "8000")),
    )


if __name__ == "__main__":
    main()

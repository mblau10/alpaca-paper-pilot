import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot import PaperPilot


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

pilot = PaperPilot()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(pilot.run_forever(), name="alpaca-paper-pilot")
    try:
        yield
    finally:
        pilot.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Alpaca Paper Pilot", lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "service": "alpaca-paper-pilot",
        "paper_only": True,
        "status_url": "/status",
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True, "paper_only": True}


@app.get("/status")
async def status():
    return pilot.public_status()


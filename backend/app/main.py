from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings
from app.scheduler.runner import setup_scheduler

settings = get_settings()
app = FastAPI(title="PPR Mini App Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
async def startup():
    scheduler = setup_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler


@app.on_event("shutdown")
async def shutdown():
    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown()

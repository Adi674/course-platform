from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from config import settings
from contextlib import asynccontextmanager
from auth.router import router as auth_router
from web_streaming.livekit.router import router as livekit_router
from .redis_client import init_redis, close_redis

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    yield
    await close_redis()

app = FastAPI(title="Live Conferencing Platform API", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "message": str(exc)},
    )

# Routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(livekit_router, prefix="/api/v1")

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

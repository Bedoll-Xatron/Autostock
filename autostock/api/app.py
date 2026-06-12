"""FastAPI 앱 인스턴스."""
from fastapi import FastAPI
from autostock.api.routes import router

app = FastAPI(title="bedoll AutoStock API", version="1.0.0")
app.include_router(router)

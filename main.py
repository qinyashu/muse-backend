from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import router
from database import init_db


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.on_event("startup")
def startup() -> None:
    """应用启动时初始化数据库表。"""
    init_db()

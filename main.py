from fastapi import FastAPI

from api import router
from database import init_db


app = FastAPI()

app.include_router(router, prefix="/api")


@app.on_event("startup")
def startup() -> None:
    """应用启动时初始化数据库表。"""
    init_db()

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from wuying.application.crawler_tasks import (
    CrawlerTaskRequest,
    CrawlerTaskService,
    PLATFORM_ID_TO_INTERNAL_PLATFORM,
    validate_platform_id,
)
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging


class GeoWatcherTaskIn(BaseModel):
    prompts: list[str] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1)
    save_name: str | None = None
    env: dict[str, Any] = Field(default_factory=dict)
    instance_id: str | None = None


class TaskAcceptedOut(BaseModel):
    task_id: str
    trace_id: str
    type: str
    status: str
    expected_records: int
    output_file: str


def create_app() -> FastAPI:
    configure_logging()
    settings = AppSettings.from_env()
    service = CrawlerTaskService(settings=settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(title="Wuying Crawler API", version="0.1.0", lifespan=lifespan)
    app.state.task_service = service

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "healthy",
            "service": "wuying-crawler",
            "platforms": sorted(PLATFORM_ID_TO_INTERNAL_PLATFORM),
        }

    @app.post("/api/v1/tasks/{platform_id}", response_model=TaskAcceptedOut)
    def create_task(
        platform_id: str,
        payload: GeoWatcherTaskIn,
        _: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        try:
            validate_platform_id(platform_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

        prompts = [prompt.strip() for prompt in payload.prompts if prompt.strip()]
        if not prompts:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="prompts cannot be empty")

        task = service.submit(
            CrawlerTaskRequest(
                platform_id=platform_id,
                prompts=prompts,
                repeat=payload.repeat,
                save_name=payload.save_name,
                env=payload.env,
                instance_id=payload.instance_id,
            )
        )
        return {
            "task_id": task["task_id"],
            "trace_id": task["trace_id"],
            "type": task["type"],
            "status": "pending",
            "expected_records": task["expected_records"],
            "output_file": task["output_file"],
        }

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
        try:
            return service.get_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found") from exc

    @app.get("/api/v1/tasks/{task_id}/results")
    def get_results(task_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
        try:
            return service.get_results(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found") from exc

    return app


def require_api_key(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    expected = os.getenv("SCRAPER_API_KEY", "").strip()
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing x-api-key")
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SCRAPER_API_KEY is not configured")
    if x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid x-api-key")


app = create_app()


__all__ = ["app", "create_app"]

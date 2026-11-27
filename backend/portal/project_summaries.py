"""Датированные сводки проекта из каталога результатов или артефактов установки."""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from . import ai_guard
from .auth import READERS, USER_ROLES, require_read
from .config import ProjectConfig, artifacts_dir
from .db import get_session
from .stands import _project


router = APIRouter(prefix="/api/projects", dependencies=[Depends(require_read(*READERS))], tags=["summaries"])
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
MANIFEST_LIMIT = 16 * 1024
REPORT_LIMIT = 2 * 1024 * 1024
NO_CACHE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=160)
    date: date
    module: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=600)


class Summary(BaseModel):
    id: str
    project: str
    title: str
    date: date
    module: str
    description: str
    has_presentation: bool


class SummaryDetail(Summary):
    markdown: str


@dataclass(frozen=True)
class Bundle:
    summary: Summary
    report: Path
    slides: Path | None


def _slug(name: str) -> bool:
    return SLUG.fullmatch(name) is not None and not ai_guard.in_restricted_dir(name)


def _member(root: Path, name: str, *, directory: bool = False) -> Path | None:
    # Разрешены только прямые дочерние файлы и каталоги, не ссылки на другой артефакт.
    if not name or name in (".", "..") or any(c in name for c in "/\\:\0"):
        return None
    path = root / name
    try:
        if path.resolve(strict=True) != path:
            return None
        return path if (path.is_dir() if directory else path.is_file()) else None
    except (OSError, RuntimeError):
        return None


def _roots(project: ProjectConfig):
    if project.results is not None:
        if root := _member(project.results.resolve(), "summaries", directory=True):
            yield root
    if root := _member(artifacts_dir().resolve(), "project-summaries", directory=True):
        if directory := _member(root, project.code, directory=True):
            yield directory


def _bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("файл превышает лимит размера")
    return data


def _bundle(root: Path, code: str, summary_id: str) -> Bundle | None:
    directory = _member(root, summary_id, directory=True)
    if directory is None:
        return None
    metadata = _member(directory, "summary.json")
    report = _member(directory, "report.md")
    if metadata is None or report is None:
        return None
    try:
        if not 0 < report.stat().st_size <= REPORT_LIMIT:
            return None
        manifest = Manifest.model_validate_json(_bounded(metadata, MANIFEST_LIMIT))
    except (OSError, ValidationError, ValueError):
        return None
    slides = _member(directory, "slides.html")
    return Bundle(
        Summary(
            id=summary_id,
            project=code,
            title=ai_guard.strip_blocks(manifest.title),
            date=manifest.date,
            module=ai_guard.strip_blocks(manifest.module),
            description=ai_guard.strip_blocks(manifest.description),
            has_presentation=slides is not None,
        ),
        report,
        slides,
    )


def _children(root: Path) -> list[Path]:
    try:
        return list(root.iterdir())
    except OSError:
        return []


def _find(project: ProjectConfig, summary_id: str) -> Bundle:
    if _slug(summary_id):
        for root in _roots(project):
            if bundle := _bundle(root, project.code, summary_id):
                return bundle
    raise HTTPException(404, "сводка не найдена")


def _markdown(bundle: Bundle) -> str:
    try:
        return ai_guard.strip_blocks(_bounded(bundle.report, REPORT_LIMIT).decode("utf-8-sig"))
    except (OSError, ValueError):
        raise HTTPException(404, "текст сводки недоступен") from None


@router.get("/{code}/summaries", response_model=list[Summary])
def list_summaries(code: str, session: Session = Depends(get_session)):
    project = _project(session, code)
    found: dict[str, Summary] = {}
    for root in _roots(project):
        for entry in _children(root):
            name = entry.name
            if name not in found and _slug(name) and (bundle := _bundle(root, code, name)):
                found[name] = bundle.summary
    return sorted(found.values(), key=lambda summary: (summary.date, summary.id), reverse=True)


@router.get("/{code}/summaries/{summary_id}", response_model=SummaryDetail)
def summary_detail(code: str, summary_id: str, session: Session = Depends(get_session)):
    bundle = _find(_project(session, code), summary_id)
    return SummaryDetail(**bundle.summary.model_dump(), markdown=_markdown(bundle))


@router.get("/{code}/summaries/{summary_id}/report", response_class=PlainTextResponse)
def download_report(code: str, summary_id: str, session: Session = Depends(get_session)):
    bundle = _find(_project(session, code), summary_id)
    return PlainTextResponse(_markdown(bundle), media_type="text/markdown", headers={
        **NO_CACHE, "Content-Disposition": f'attachment; filename="{summary_id}.md"',
    })


@router.get("/{code}/summaries/{summary_id}/presentation", dependencies=[Depends(require_read(*USER_ROLES))])
def download_presentation(code: str, summary_id: str, session: Session = Depends(get_session)):
    bundle = _find(_project(session, code), summary_id)
    if bundle.slides is None:
        raise HTTPException(404, "презентации нет")
    return FileResponse(bundle.slides, media_type="text/html", filename=f"{summary_id}-slides.html", headers={
        **NO_CACHE, "Content-Security-Policy": "sandbox; default-src 'none'",
    })

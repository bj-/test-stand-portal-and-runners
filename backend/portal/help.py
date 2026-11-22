"""Справка установки: полезные ссылки и страницы-инструкции на Markdown (блок на «Обзоре», страницы /help/<имя>).

Всё содержимое - из конфигурации установки (раздел help файла проектов): в коде портала нет ничего о клиенте.
Страница - файл Markdown на машине портала; портал отдаёт только файлы, перечисленные в конфигурации.
"""
import os
import re
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import USER_ROLES, require_read

router = APIRouter(prefix="/api/help", dependencies=[Depends(require_read(*USER_ROLES))])
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


class HelpItem(BaseModel):
    title: str
    url: str  # внешняя ссылка или /help/<страница>
    note: str = ""


class HelpSection(BaseModel):
    title: str
    items: list[HelpItem]


class HelpPage(BaseModel):
    title: str
    markdown: str


def _config() -> dict:
    file = Path(os.environ.get("PORTAL_PROJECTS", "projects.yaml"))
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return data.get("help") or {}


def _pages() -> dict[str, dict]:
    return {slug: page for slug, page in (_config().get("pages") or {}).items() if SLUG.match(str(slug))}


@router.get("", response_model=list[HelpSection])
def sections():
    pages = _pages()
    result = []
    for section in _config().get("sections") or []:
        items = []
        for item in section.get("items") or []:
            if item.get("page"):
                if item["page"] not in pages:
                    continue  # страница не описана - ссылку не показываем
                url = f"/help/{item['page']}"
            else:
                url = str(item.get("url", ""))
            items.append(HelpItem(title=item.get("title", url), url=url, note=item.get("note", "")))
        if items:
            result.append(HelpSection(title=section.get("title", ""), items=items))
    return result


@router.get("/pages/{slug}", response_model=HelpPage)
def page(slug: str):
    found = _pages().get(slug)
    if found is None:
        raise HTTPException(404, "страница не найдена")
    try:
        text = Path(found["file"]).read_text(encoding="utf-8")
    except (KeyError, OSError):
        raise HTTPException(404, "файл страницы недоступен") from None
    return HelpPage(title=found.get("title", slug), markdown=text)

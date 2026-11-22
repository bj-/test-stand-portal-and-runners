"""Закрытое от ИИ-ассистента (токен llm): те же правила, что у движка тестов (ADR Test-Library 0008, ai_guard движка).

- каталоги `restricted/` и части прогона группы `sg-restricted` - не отдаются совсем;
- блоки `<!-- ai-restricted:begin -->` ... `<!-- ai-restricted:end -->` в отчёте заменяются пометкой.
Люди видят всё. Правила менять вместе с движком: портал и `autotest.py view` должны показывать ассистенту одно и то же.
"""
import re
from pathlib import PurePosixPath

BEGIN, END = "<!-- ai-restricted:begin -->", "<!-- ai-restricted:end -->"
BLOCK = re.compile(re.escape(BEGIN) + r".*?(?:" + re.escape(END) + r"|\Z)", re.S)
HIDDEN = "[скрыто: ai-restricted - читает человек]"
RESTRICTED_GROUP = "sg-restricted"


def in_restricted_dir(path: str) -> bool:
    return bool({"restricted", RESTRICTED_GROUP} & {p.lower() for p in PurePosixPath(path.replace("\\", "/")).parts})


def strip_blocks(text: str) -> str:
    return BLOCK.sub(HIDDEN, text)

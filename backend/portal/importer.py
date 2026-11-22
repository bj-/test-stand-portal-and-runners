"""Импорт репозитория результатов в индекс БД. Идемпотентен: неизменённые файлы не трогает."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ProjectConfig
from .models import Bug, Project, Run, TestResult

RESTRICTED_BLOCK = re.compile(r"<!--\s*ai-restricted:begin\s*-->.*?<!--\s*ai-restricted:end\s*-->", re.S)
HEADER_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$")
RUN_LINK = re.compile(r"\[?([A-Z0-9]+-\d{8}-\d{6}-[^\]\s/|)]+)")


@dataclass
class ImportStats:
    runs_added: int = 0
    runs_updated: int = 0
    runs_skipped: int = 0
    bugs_added: int = 0
    bugs_updated: int = 0
    bugs_skipped: int = 0


def _digest(*parts: bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
    return h.hexdigest()


def _time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if value else None


def _read_optional(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def import_run(session: Session, project: ProjectConfig, directory: Path, stats: ImportStats) -> None:
    manifest_raw = (directory / "run.yaml").read_bytes()
    results_raw = _read_optional(directory / "results.json")
    report_raw = _read_optional(directory / "report.md")
    source_hash = _digest(manifest_raw, results_raw, report_raw)

    manifest = yaml.safe_load(manifest_raw)
    run = session.get(Run, manifest["id"])
    if run is not None and run.source_hash == source_hash:
        stats.runs_skipped += 1
        return
    if run is None:
        run = Run(id=manifest["id"], project=project.code)
        session.add(run)
        stats.runs_added += 1
    else:
        stats.runs_updated += 1

    summary = manifest.get("summary") or {}
    stand = manifest.get("stand") or {}
    tests = manifest.get("tests") or {}
    run.suite = Path(manifest.get("suite") or directory.name).stem
    run.kind = manifest.get("kind")
    run.modules = manifest.get("modules") or []
    run.started = _time(manifest.get("started"))
    run.finished = _time(manifest.get("finished"))
    run.who = manifest.get("who")
    run.trigger = (manifest.get("trigger") or {}).get("type")
    run.stand_name = stand.get("environment")
    run.stand_url = stand.get("url")
    run.stand_kind = stand.get("kind")
    run.build = (manifest.get("product") or {}).get("build")
    run.tests_branch = tests.get("branch")
    run.tests_commit = tests.get("commit")
    run.engine_version = (manifest.get("engine") or {}).get("version")
    run.runner = manifest.get("runner")
    run.gate_failed = bool(manifest.get("gate_failed"))
    run.tests = summary.get("tests", 0)
    run.passed = summary.get("passed", 0)
    run.failed = summary.get("failed", 0)
    run.errors = summary.get("error", 0)
    run.no_checks = summary.get("no-checks", 0)
    run.skipped = summary.get("skipped", 0)
    run.checks = summary.get("checks", 0)
    run.checks_failed = summary.get("checks_failed", 0)
    run.bug_ids = sorted({m.decode() for m in re.findall(rb"BUG-[A-Z0-9]+-\d{4}", report_raw)})
    run.path = directory.relative_to(project.results).as_posix()
    run.source_hash = source_hash

    # Тела запросов и ответов остаются в results.json: в индексе только то, по чему ищем и считаем.
    executions = json.loads(results_raw).get("executions", []) if results_raw else []
    run.results = [
        TestResult(
            position=index,
            path=item.get("path", ""),
            name=item.get("name", ""),
            method=item.get("method"),
            url=item.get("url"),
            code=item.get("code"),
            time_ms=item.get("time_ms"),
            verdict=item.get("verdict", "unknown"),
            gate=bool(item.get("gate")),
            groups=item.get("groups") or [],
        )
        for index, item in enumerate(executions)
    ]
    session.flush()


def split_restricted(text: str) -> tuple[str, str | None]:
    blocks = [m.group(0) for m in RESTRICTED_BLOCK.finditer(text)]
    if not blocks:
        return text, None
    return RESTRICTED_BLOCK.sub("[закрыто: AI-ограничение]", text), "\n\n".join(blocks)


def parse_bug(text: str) -> dict:
    title_line = next((line for line in text.splitlines() if line.startswith("# ")), "")
    match = re.match(r"#\s*(BUG-([A-Z0-9]+)-(\d+))\.?\s*(.*)", title_line)
    if not match:
        raise ValueError(f"нет заголовка BUG-<КОД>-NNNN: {title_line!r}")
    header = {}
    for line in text.splitlines():
        row = HEADER_ROW.match(line)
        if row:
            header[row.group(1)] = row.group(2)
        elif header and line.startswith("#"):
            break
    found = header.get("Найден", "")
    date = re.match(r"\d{4}-\d{2}-\d{2}", found)
    run = RUN_LINK.search(found)
    tracker = header.get("Трекер", "").strip()
    groups = [g.strip() for g in header.get("Группа", "").split(",") if g.strip()]
    return {
        "id": match.group(1),
        "number": int(match.group(3)),
        "title": match.group(4).strip(),
        "module": header.get("Модуль"),
        "type": header.get("Тип"),
        "severity": (header.get("Серьёзность") or "").split(" ")[0] or None,
        "priority": header.get("Приоритет"),
        "status": header.get("Статус"),
        "restricted": header.get("AI-доступ", "").strip().strip("`") == "restricted",
        "groups": groups,
        "stand": header.get("Стенд"),
        "found_date": date.group(0) if date else None,
        "run_id": run.group(1) if run else None,
        "tracker": None if tracker in ("", "-") else tracker,
    }


def import_bug(session: Session, project: ProjectConfig, file: Path, stats: ImportStats) -> None:
    raw = file.read_bytes()
    fields = parse_bug(raw.decode("utf-8"))
    restricted_file = project.results / "bugs" / "restricted" / f"{fields['id']}.md"
    extra = _read_optional(restricted_file)
    source_hash = _digest(raw, extra)

    bug = session.get(Bug, fields["id"])
    if bug is not None and bug.source_hash == source_hash:
        stats.bugs_skipped += 1
        return
    if bug is None:
        bug = Bug(id=fields["id"], project=project.code)
        session.add(bug)
        stats.bugs_added += 1
    else:
        stats.bugs_updated += 1

    public, closed = split_restricted(raw.decode("utf-8"))
    if extra:
        closed = "\n\n".join(filter(None, [closed, extra.decode("utf-8")]))
    for key, value in fields.items():
        setattr(bug, key, value)
    bug.body = public
    bug.restricted_body = closed
    bug.source_hash = source_hash
    session.flush()


def sync_project(session: Session, project: ProjectConfig) -> ImportStats:
    if project.results is None:
        raise ValueError("репозиторий результатов проекта не настроен")
    stats = ImportStats()
    existing = session.get(Project, project.code)
    if existing is None:
        session.add(Project(code=project.code, title=project.title))
        session.flush()
    else:
        existing.title = project.title

    seen = set()
    for manifest in sorted(project.results.glob("runs/*/*/*/run.yaml")):
        import_run(session, project, manifest.parent, stats)
        seen.add(manifest.parent.name)

    # Прогон удалён из репозитория - убираем и из индекса: индекс не хранит ничего своего.
    for run in session.scalars(select(Run).where(Run.project == project.code)).all():
        if run.id not in seen:
            session.delete(run)

    for file in sorted((project.results / "bugs").glob("BUG-*.md")):
        import_bug(session, project, file, stats)
    session.commit()
    return stats

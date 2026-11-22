"""Проектный комплект и неизменяемые ссылки на данные управляемого стенда."""
import copy
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .auth import now
from .models import Dataset, DatasetVersion, Project
from .stand_contract import ID
from .test_data import DatasetIn, MAX_DATA, validate_json

MAX_ENTRIES = 64
DEFAULT_TITLE = "Базовый комплект"
MODULE = re.compile("^" + ID.pattern)


class BundleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def clean_strings(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("нужен корректный Unicode") from None
            if not value.strip() or "\x00" in value:
                raise ValueError("нужна непустая строка без NUL")
        return value


class BundleEntryIn(BundleInput):
    module: str = Field(min_length=1, max_length=128, pattern=MODULE)
    dataset_id: int = Field(gt=0)
    version: int = Field(gt=0)


class BundleIn(BundleInput):
    title: str = Field(min_length=1, max_length=200)
    entries: list[BundleEntryIn] = Field(max_length=MAX_ENTRIES)

    @model_validator(mode="after")
    def unique_modules(self):
        modules = [entry.module for entry in self.entries]
        if len(set(modules)) != len(modules):
            raise ValueError("модуль повторяется в комплекте")
        return self


class InitialDatasetIn(DatasetIn):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: str = Field(min_length=1, max_length=128, pattern=MODULE)


def _encoded_size(data):
    validate_json(data)
    return len(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _check_total(total):
    if total > MAX_DATA:
        raise HTTPException(422, "данные комплекта превышают 2 MiB в UTF-8")


def _entry(session, code, item):
    dataset = session.get(Dataset, item.dataset_id)
    if dataset is None or dataset.project != code:
        raise HTTPException(422, "набор комплекта не найден в проекте")
    version = session.get(DatasetVersion, (dataset.id, item.version))
    if version is None:
        raise HTTPException(422, "версия набора комплекта не найдена")
    return dataset, version


def resolve_bundle(session, code, body: BundleIn):
    entries, total = [], 0
    for item in body.entries:
        dataset, version = _entry(session, code, item)
        total += _encoded_size(version.data)
        _check_total(total)
        entries.append({**item.model_dump(), "title": dataset.title, "key": dataset.key, "format": dataset.format})
    return {"title": body.title, "entries": entries}


def project_bundle(session, code):
    row = session.get(Project, code)
    stored = row.stand_data_bundle if row is not None else None
    if stored is None:
        return {"title": DEFAULT_TITLE, "entries": []}
    # Persisted metadata is projected explicitly: no raw payload reaches this API.
    refs = {"title": stored["title"], "entries": [
        {key: item[key] for key in ("module", "dataset_id", "version")} for item in stored["entries"]]}
    return resolve_bundle(session, code, BundleIn.model_validate(refs))


def public_bundle(plan):
    if plan is None:
        return None
    return {"title": plan["title"], "entries": [
        {key: item[key] for key in ("module", "dataset_id", "version", "title", "key", "format")}
        for item in plan["entries"]]}


def initialize_bundle(session, project, items, principal):
    """The caller commits the project, datasets, versions and bundle together."""
    modules, keys, entries, total = set(), set(), [], 0
    for item in items:
        if item.module in modules or item.key in keys:
            raise HTTPException(422, "модуль или ключ набора повторяется")
        modules.add(item.module)
        keys.add(item.key)
        total += _encoded_size(item.data)
        _check_total(total)
    for item in items:
        dataset = Dataset(project=project.code, **item.model_dump(exclude={"module", "data"}),
                          version=1, created_by=principal.name, created=now())
        session.add(dataset)
        session.flush()
        session.add(DatasetVersion(dataset_id=dataset.id, version=1, data=copy.deepcopy(item.data),
                                   created_by=principal.name, created=now()))
        entries.append({"module": item.module, "dataset_id": dataset.id, "version": 1,
                        "title": dataset.title, "key": dataset.key, "format": dataset.format})
    project.stand_data_bundle = {"title": DEFAULT_TITLE, "entries": entries}


def applicable_bundle(session, code, template, profile, install=True):
    if not install:
        return {"title": DEFAULT_TITLE, "entries": []}
    bundle = project_bundle(session, code)
    modules = template.get("data_modules", {})
    services = set(template["profiles"][profile]["services"])
    entries = []
    for item in bundle["entries"]:
        adapter = modules.get(item["module"])
        if adapter is None or adapter["service"] not in services:
            continue
        if item["format"] != adapter["format"]:
            raise HTTPException(422, f"формат набора модуля {item['module']} не совпадает с адаптером шаблона")
        entries.append(item)
    return {"title": bundle["title"], "entries": entries}


def seed_payload(session, job):
    """Resolve the pinned plan immediately before a successful worker claim."""
    plan = job.spec.get("test_data")
    if plan is None:
        return []
    try:
        refs = BundleIn.model_validate({"title": plan["title"], "entries": [
            {key: item[key] for key in ("module", "dataset_id", "version")} for item in plan["entries"]]})
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "неверный сохранённый комплект данных") from None
    entries, total = [], 0
    for reference, metadata in zip(refs.entries, plan["entries"]):
        dataset, version = _entry(session, job.project, reference)
        if metadata.get("format") != dataset.format:
            raise HTTPException(422, "формат сохранённого набора изменился")
        total += _encoded_size(version.data)
        _check_total(total)
        entries.append({**reference.model_dump(), "format": dataset.format, "data": copy.deepcopy(version.data)})
    return entries

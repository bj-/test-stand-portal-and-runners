"""Pure v1 JSON contract, copied from test-stand-portal-and-runners/stands/stand.py.

No lifecycle command is executed. Validation checks shapes and named secret references;
it does not inspect arbitrary string values for possible secrets.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath
import re

STAGES = ("build", "deploy", "migrate", "seed", "healthcheck", "smoke")
LIVENESS_STAGE = "liveness"
LIVENESS_CAPABILITY = "stand-template-v1-liveness"
DATA_CAPABILITY = "stand-template-v1-data"
DATA_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")


class StandError(ValueError):
    """Invalid input or an unsafe/unavailable preparation operation."""


def fail(message):
    raise StandError(message)


def obj(value, required, optional=(), label="object"):
    if not isinstance(value, dict):
        fail(f"{label}: expected object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        fail(f"{label}: missing fields {sorted(missing)}, unknown fields {sorted(unknown)}")
    return value


def string(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"{label}: expected nonempty string without NUL")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value) or value in (".", ".."):
        fail(f"{label}: invalid identifier")
    return value


def strings(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        fail(f"{label}: expected {'nonempty ' if nonempty else ''}array")
    for item in value:
        string(item, label)
    if len(set(value)) != len(value):
        fail(f"{label}: duplicate values")
    return value


def version(value, label):
    if type(value) is not int or value != 1:
        fail(f"{label}: only schema_version=1 is supported")


def ref_name(value):
    string(value, "ref")
    if SHA.fullmatch(value):
        return value
    if value in {"HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "AUTO_MERGE", "@"}:
        fail("ref: symbolic revisions are not supported")
    if (value.startswith(("-", "/")) or value.endswith(("/", "."))
            or any(ord(c) <= 32 or ord(c) == 127 or c in "~^:?*[\\" for c in value)
            or ".." in value or "//" in value or "@{" in value):
        fail("ref: expected a branch, tag, or full commit SHA")
    if value.startswith("refs/") and not value.startswith(("refs/heads/", "refs/tags/")):
        fail("ref: only refs/heads and refs/tags are supported")
    if value in {"refs/heads/", "refs/tags/"}:
        fail("ref: empty branch/tag")
    if any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        fail("ref: invalid branch/tag name")
    return value


def relative_path(value, label):
    string(value, label)
    parts = value.split("/")
    if (value.startswith("/") or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        fail(f"{label}: expected a portable relative path without traversal")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{s}{i}" for s in ("COM", "LPT") for i in range(1, 10)}
    for part in parts:
        if (part.rstrip(". ") != part or part.upper().split(".")[0] in reserved
                or part.casefold() == ".git" or any(c in '<>"|?*' for c in part)):
            fail(f"{label}: nonportable or reserved path")
    return PurePosixPath(value)


def typed(value, kind, label):
    valid = {"string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float), "boolean": type(value) is bool}
    if not isinstance(kind, str) or kind not in valid or not valid[kind]:
        fail(f"{label}: expected {kind}")
    if isinstance(value, str) and "\x00" in value:
        fail(f"{label}: NUL is forbidden")
    if isinstance(value, float) and not math.isfinite(value):
        fail(f"{label}: nonfinite number")


def _validate_command(command, components):
    obj(command, ("component", "argv"), label="command")
    if identifier(command["component"], "command.component") not in components:
        fail("command.component: unknown component")
    if not isinstance(command["argv"], list) or not command["argv"]:
        fail("argv: expected nonempty array")
    for arg in command["argv"]:
        if not isinstance(arg, str) or "\x00" in arg:
            fail("argv: strings without NUL required")
    string(command["argv"][0], "argv[0]")


def validate_data_modules(modules, services, bindings):
    if not isinstance(modules, dict) or not 1 <= len(modules) <= 64:
        fail("data_modules: expected 1..64 modules")
    seen_modules, seen_bindings = set(), set()
    for module, adapter in modules.items():
        relative_path(identifier(module, "data module"), "data module")
        if module.casefold() in seen_modules:
            fail("data_modules: case-colliding module")
        seen_modules.add(module.casefold())
        obj(adapter, ("service", "format", "argv"), ("title", "run_binding"), label="data adapter")
        if "title" in adapter:
            title = string(adapter["title"], "data module title")
            if len(title) > 200 or not title.strip() or not title.isprintable():
                fail("data module title: expected 1..200 printable characters")
        if identifier(adapter["service"], "data service") not in services:
            fail("data module: service must be enabled in a profile")
        if not isinstance(adapter["format"], str) or not DATA_FORMAT.fullmatch(adapter["format"]):
            fail("data module: invalid format")
        argv = adapter["argv"]
        if not isinstance(argv, list) or not argv:
            fail("data module argv: expected nonempty array")
        for arg in argv:
            string(arg, "data module argv")
        if argv[0].startswith("-"):
            fail("data module argv: expected a container command")
        if "run_binding" in adapter:
            binding = identifier(adapter["run_binding"], "data module run_binding")
            relative_path(binding, "data module run_binding")
            if ".." in binding or binding not in bindings or binding.casefold() in seen_bindings:
                fail("data module: run_binding must be unique and refer to an output binding")
            seen_bindings.add(binding.casefold())


def validate_template(t):
    obj(t, ("schema_version", "id", "version", "components", "compose", "profiles",
            "lifecycle", "parameters", "secret_refs", "smoke", "output_bindings"), (LIVENESS_STAGE, "data_modules"), label="template")
    version(t["schema_version"], "template")
    identifier(t["id"], "template.id")
    if not isinstance(t["version"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", t["version"]):
        fail("template.version: invalid version label")
    if not isinstance(t["components"], list) or not t["components"]:
        fail("components: expected nonempty array")
    components = {}
    for c in t["components"]:
        obj(c, ("id", "repository", "default_ref", "dependencies"), label="component")
        cid = identifier(c["id"], "component.id")
        relative_path(cid, "component.id")
        identifier(c["repository"], "component.repository")
        ref_name(c["default_ref"])
        strings(c["dependencies"], "dependencies")
        if cid in components or cid.casefold() in {key.casefold() for key in components}:
            fail("components: duplicate/case-colliding ID")
        components[cid] = c
    visiting, visited = set(), set()

    def visit(cid):
        if cid not in components:
            fail(f"dependencies: missing component {cid}")
        if cid in visiting:
            fail("dependencies: cycle")
        if cid in visited:
            return
        visiting.add(cid)
        for dependency in components[cid]["dependencies"]:
            visit(dependency)
        visiting.remove(cid)
        visited.add(cid)

    for cid in components:
        visit(cid)
    compose = obj(t["compose"], ("component", "path", "required_services"), label="compose")
    if identifier(compose["component"], "compose.component") not in components:
        fail("compose.component: unknown component")
    relative_path(compose["path"], "compose.path")
    required = strings(compose["required_services"], "required_services", nonempty=True)
    for service in required:
        identifier(service, "service")
    if not isinstance(t["profiles"], dict) or "laptop" not in t["profiles"]:
        fail("profiles: laptop is required")
    profile_services = set()
    for name, profile in t["profiles"].items():
        identifier(name, "profile")
        obj(profile, ("services", "resources"), label="profile")
        services = strings(profile["services"], "profile.services", nonempty=True)
        for service in services:
            identifier(service, "service")
        if not set(required).issubset(services):
            fail("profile.services: required services must be included")
        profile_services.update(services)
        resources = obj(profile["resources"], ("cpus", "memory_mb"), label="resources")
        typed(resources["cpus"], "number", "cpus")
        typed(resources["memory_mb"], "integer", "memory_mb")
        if resources["cpus"] <= 0 or resources["memory_mb"] <= 0:
            fail("resources: limits must be positive")
    obj(t["lifecycle"], STAGES, label="lifecycle")
    for stage in STAGES:
        commands = t["lifecycle"][stage]
        if not isinstance(commands, list) or (not commands and stage not in ("migrate", "seed")):
            fail(f"lifecycle.{stage}: expected commands (migrate/seed may be empty)")
        for command in commands:
            _validate_command(command, components)
    if LIVENESS_STAGE in t:
        checks = t[LIVENESS_STAGE]
        if not isinstance(checks, dict) or not checks:
            fail("liveness: expected a nonempty object keyed by service")
        for service, command in checks.items():
            if identifier(service, "liveness service") not in profile_services:
                fail("liveness: service must be enabled in a profile")
            _validate_command(command, components)
    if not isinstance(t["parameters"], dict):
        fail("parameters: expected object")
    for name, spec in t["parameters"].items():
        identifier(name, "parameter")
        obj(spec, ("type",), ("default", "enum"), label="parameter specification")
        if spec["type"] not in ("string", "integer", "number", "boolean"):
            fail("parameter.type: unsupported type")
        if "enum" in spec:
            if not isinstance(spec["enum"], list) or not spec["enum"]:
                fail("parameter.enum: expected nonempty array")
            for value in spec["enum"]:
                typed(value, spec["type"], "parameter.enum")
        if "default" in spec:
            validate_parameter(spec["default"], spec, name)
    strings(t["secret_refs"], "secret_refs")
    for name in t["secret_refs"]:
        identifier(name, "secret reference name")
    smoke = obj(t["smoke"], ("runner", "env", "suite"), label="smoke")
    if identifier(smoke["runner"], "smoke.runner") not in components:
        fail("smoke.runner: expected component ID")
    env = smoke["env"]
    if not isinstance(env, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", env):
        fail("smoke.env: expected a safe environment filename segment")
    relative_path(env, "smoke.env")
    relative_path(smoke["suite"], "smoke.suite")
    if not isinstance(t["output_bindings"], dict):
        fail("output_bindings: expected object")
    for name, binding in t["output_bindings"].items():
        identifier(name, "output binding")
        obj(binding, ("service", "port", "scheme", "path"), ("title",), label="output binding")
        if "title" in binding:
            title = string(binding["title"], "output binding.title")
            if not title.strip() or len(title) > 200 or not title.isprintable():
                fail("output binding.title: expected 1..200 printable characters")
        if identifier(binding["service"], "binding.service") not in required:
            fail("output binding: service must be required in every profile")
        if type(binding["port"]) is not int or not 1 <= binding["port"] <= 65535:
            fail("output binding.port: expected integer 1..65535")
        if binding["scheme"] not in ("http", "https"):
            fail("output binding.scheme: expected http or https")
        path = string(binding["path"], "output binding.path")
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n?#\\"):
            fail("output binding.path: expected absolute URL path")
    if "data_modules" in t:
        validate_data_modules(t["data_modules"], profile_services, t["output_bindings"])
    return t


def binding_titles(template, overrides=None):
    overrides = overrides or {}
    return {name: binding.get("title") or overrides.get(name) or binding["service"]
            for name, binding in template.get("output_bindings", {}).items()}


def validate_parameter(value, spec, name):
    typed(value, spec["type"], f"parameter {name}")
    if "enum" in spec and value not in spec["enum"]:
        fail(f"parameter {name}: value outside enum")


def validate_request(t, request, repositories):
    obj(request, ("schema_version", "profile", "components", "parameters"), label="request")
    version(request["schema_version"], "request")
    if not isinstance(request["profile"], str) or request["profile"] not in t["profiles"]:
        fail("request.profile: unknown profile")
    obj(request["components"], (), [c["id"] for c in t["components"]], label="request.components")
    selected = []
    for component in t["components"]:
        cid = component["id"]
        choice = request["components"].get(cid, {})
        obj(choice, (), ("repository", "ref"), label="request component")
        rid = choice.get("repository", component["repository"])
        if not isinstance(rid, str) or rid not in repositories or cid not in repositories[rid]["components"]:
            fail(f"component {cid}: repository missing or incompatible")
        selected.append({"id": cid, "repository": rid,
                         "requested_ref": ref_name(choice.get("ref", component["default_ref"]))})
    obj(request["parameters"], (), t["parameters"], label="request.parameters")
    parameters = {}
    for name, spec in t["parameters"].items():
        if name not in request["parameters"] and "default" not in spec:
            fail(f"parameter {name}: required")
        value = request["parameters"].get(name, spec.get("default"))
        validate_parameter(value, spec, name)
        parameters[name] = value
    return selected, parameters


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def template_hash(template):
    return hashlib.sha256(canonical(template)).hexdigest()


def validate_snapshot(snapshot, repositories):
    obj(snapshot, ("schema_version", "template_sha256", "template", "components", "profile",
                   "parameters", "secret_refs"), label="snapshot")
    version(snapshot["schema_version"], "snapshot")
    t = validate_template(snapshot["template"])
    if snapshot["template_sha256"] != template_hash(t):
        fail("snapshot: frozen template hash mismatch")
    if snapshot["secret_refs"] != t["secret_refs"]:
        fail("snapshot: secret reference names differ from frozen template")
    if not isinstance(snapshot["components"], list):
        fail("snapshot.components: expected array")
    choices = {}
    for c in snapshot["components"]:
        obj(c, ("id", "repository", "requested_ref", "sha"), label="snapshot component")
        cid = identifier(c["id"], "snapshot component ID")
        if cid in choices:
            fail("snapshot: duplicate component")
        if not isinstance(c["sha"], str) or not SHA.fullmatch(c["sha"]):
            fail("snapshot: full SHA required")
        if (isinstance(c["requested_ref"], str) and SHA.fullmatch(c["requested_ref"])
                and c["requested_ref"].lower() != c["sha"].lower()):
            fail("snapshot: commit differs from explicitly requested SHA")
        choices[cid] = {"repository": c["repository"], "ref": c["requested_ref"]}
    if set(choices) != {c["id"] for c in t["components"]}:
        fail("snapshot: component set differs from frozen template")
    validate_request(t, {"schema_version": 1, "components": choices,
                         "profile": snapshot["profile"], "parameters": snapshot["parameters"]}, repositories)
    if set(snapshot["parameters"]) != set(t["parameters"]):
        fail("snapshot: resolved parameters must be complete")
    return t

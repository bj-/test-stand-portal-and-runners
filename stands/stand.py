"""Validate trusted JSON templates and export immutable, isolated Git contexts.

Python 3.12 standard library only. No template command is executed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


class StandError(ValueError):
    """Invalid input or an unsafe/unavailable preparation operation."""


STAGES = ("build", "deploy", "migrate", "seed", "healthcheck", "smoke")
ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MARKER = ".stand-build-v1"


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


def validate_template(t):
    obj(t, ("schema_version", "id", "version", "components", "compose", "profiles",
            "lifecycle", "parameters", "secret_refs", "smoke", "output_bindings"), label="template")
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
    for name, profile in t["profiles"].items():
        identifier(name, "profile")
        obj(profile, ("services", "resources"), label="profile")
        services = strings(profile["services"], "profile.services", nonempty=True)
        for service in services:
            identifier(service, "service")
        if not set(required).issubset(services):
            fail("profile.services: required services must be included")
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
            obj(command, ("component", "argv"), label="command")
            if identifier(command["component"], "command.component") not in components:
                fail("command.component: unknown component")
            if not isinstance(command["argv"], list) or not command["argv"]:
                fail("argv: expected nonempty array")
            for arg in command["argv"]:
                if not isinstance(arg, str) or "\x00" in arg:
                    fail("argv: strings without NUL required")
            string(command["argv"][0], "argv[0]")
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
    return t


def validate_parameter(value, spec, name):
    typed(value, spec["type"], f"parameter {name}")
    if "enum" in spec and value not in spec["enum"]:
        fail(f"parameter {name}: value outside enum")


def git(path, *args, binary=False):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "-C", str(path), *args], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=20)
    except subprocess.TimeoutExpired:
        fail("Git operation exceeded 20 seconds")
    if result.returncode:
        fail(f"Git operation failed: {args[0]} (exit {result.returncode})")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def validate_catalog(catalog):
    obj(catalog, ("schema_version", "repositories"), label="catalog")
    version(catalog["schema_version"], "catalog")
    if not isinstance(catalog["repositories"], list) or not catalog["repositories"]:
        fail("catalog.repositories: expected nonempty array")
    repositories = {}
    for entry in catalog["repositories"]:
        obj(entry, ("id", "path", "components"), label="repository")
        rid = identifier(entry["id"], "repository.id")
        if rid in repositories:
            fail("catalog: duplicate repository ID")
        path = Path(string(entry["path"], "repository.path"))
        if not path.is_absolute() or not path.is_dir():
            fail("repository.path: existing absolute clone path required")
        path = path.resolve()
        if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
            fail("repository.path: clone root required")
        for cid in strings(entry["components"], "repository.components", nonempty=True):
            identifier(cid, "repository component")
        repositories[rid] = {"path": path, "components": entry["components"]}
    return repositories


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


def resolve_ref(path, requested):
    if SHA.fullmatch(requested):
        resolved = git(path, "rev-parse", "--verify", "--end-of-options", requested + "^{commit}")
        if resolved.lower() != requested.lower():
            fail("full SHA must identify a commit, not an annotated tag object")
    else:
        candidates = ([requested] if requested.startswith("refs/") else
                      ["refs/heads/" + requested, "refs/tags/" + requested])
        found = git(path, "for-each-ref", "--format=%(refname)", *candidates).splitlines()
        exact = [name for name in found if name in candidates]
        if len(exact) != 1:
            fail(f"ref {requested}: missing or ambiguous branch/tag; no fallback")
        resolved = git(path, "rev-parse", "--verify", "--end-of-options", exact[0] + "^{commit}")
    if not SHA.fullmatch(resolved):
        fail("Git did not return a full commit SHA")
    return resolved.lower()


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
        requested = ref_name(c["requested_ref"])
        if SHA.fullmatch(requested) and requested.lower() != c["sha"].lower():
            fail("snapshot: explicit requested SHA differs from frozen commit SHA")
        choices[cid] = {"repository": c["repository"], "ref": c["requested_ref"]}
    if set(choices) != {c["id"] for c in t["components"]}:
        fail("snapshot: component set differs from frozen template")
    validate_request(t, {"schema_version": 1, "components": choices,
                         "profile": snapshot["profile"], "parameters": snapshot["parameters"]}, repositories)
    if set(snapshot["parameters"]) != set(t["parameters"]):
        fail("snapshot: resolved parameters must be complete")
    return t


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def contains(parent, child):
    return child == parent or parent in child.parents


def check_workspace(workspace, repositories):
    raw = Path(workspace).absolute()
    for part in (raw, *raw.parents):
        if is_link(part):
            fail("workspace: symlink/junction in destination path")
    target = raw.resolve()
    if not target.parent.is_dir():
        fail("workspace: parent directory must already exist")
    roots = [SOURCE_ROOT, *(repo["path"] for repo in repositories.values())]
    for root in roots:
        if contains(root, target) or contains(target, root):
            fail("workspace: must be outside source clones and tool repository, without containment")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        fail("workspace: target must be new or empty; existing builds are never overwritten")
    return target


def export_commit(path, sha, destination):
    # Read Git objects, never the checkout: dirty files, HEAD and index are irrelevant.
    tree = git(path, "ls-tree", "-rz", "--full-tree", sha, binary=True)
    entries, seen = [], {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            fail("Git tree: UTF-8 filenames required")
        relative = relative_path(name, "Git tree path")
        if mode not in ("100644", "100755") or kind != "blob":
            fail("Git tree: symlinks, submodules and special entries are unsupported")
        for count in range(1, len(relative.parts) + 1):
            prefix = "/".join(relative.parts[:count])
            folded = prefix.casefold()
            if folded in seen and seen[folded] != prefix:
                fail("Git tree: case-colliding paths")
            seen[folded] = prefix
        entries.append((relative, oid, mode))
    # All paths and modes are checked before writing any blob.
    destination.mkdir()
    for relative, oid, mode in entries:
        output = destination.joinpath(*relative.parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(git(path, "cat-file", "blob", oid, binary=True))
        if os.name != "nt" and mode == "100755":
            output.chmod(0o755)


def publish(snapshot, repositories, workspace):
    target = check_workspace(workspace, repositories)
    stage = Path(tempfile.mkdtemp(prefix=".stand-stage-", dir=target.parent))
    (stage / MARKER).write_text("stand-build-v1\n", encoding="utf-8")
    try:
        contexts = stage / "contexts"
        contexts.mkdir()
        for component in snapshot["components"]:
            export_commit(repositories[component["repository"]]["path"], component["sha"],
                          contexts / component["id"])
        compose = snapshot["template"]["compose"]
        if not (contexts / compose["component"] / compose["path"]).is_file():
            fail("compose.path: committed file is missing from selected component")
        (stage / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Recheck immediately before publication. rmdir only consumes an empty target;
        # no existing contents are removed. Rename stays on the same filesystem.
        check_workspace(target, repositories)
        if target.exists():
            target.rmdir()
        stage.rename(target)
        return target / "snapshot.json"
    finally:
        if stage.exists():
            # Only our freshly allocated staging directory is ever recursively removed.
            shutil.rmtree(stage)


def prepare(template, catalog, request, workspace):
    t = validate_template(template)
    repositories = validate_catalog(catalog)
    selected, parameters = validate_request(t, request, repositories)
    check_workspace(workspace, repositories)
    for component in selected:
        component["sha"] = resolve_ref(repositories[component["repository"]]["path"], component["requested_ref"])
    snapshot = {"schema_version": 1, "template_sha256": template_hash(t), "template": t,
                "components": selected, "profile": request["profile"], "parameters": parameters,
                "secret_refs": t["secret_refs"]}
    return publish(snapshot, repositories, workspace)


def replay(snapshot, catalog, workspace):
    repositories = validate_catalog(catalog)
    validate_snapshot(snapshot, repositories)
    check_workspace(workspace, repositories)
    for component in snapshot["components"]:
        # Deliberately never resolve requested_ref during replay.
        resolved = resolve_ref(repositories[component["repository"]]["path"], component["sha"])
        if resolved != component["sha"].lower():
            fail("snapshot: commit SHA mismatch")
    return publish(snapshot, repositories, workspace)


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail(f"JSON: duplicate key {key}")
            result[key] = value
        return result

    def constant(_value):
        fail("JSON: nonfinite numbers are forbidden")

    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=pairs, parse_constant=constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "prepare", "replay"):
        child = sub.add_parser(command)
        if command != "replay":
            child.add_argument("--template", required=True)
            child.add_argument("--request", required=command == "prepare")
        else:
            child.add_argument("--snapshot", required=True)
        child.add_argument("--catalog", required=command != "validate")
        if command != "validate":
            child.add_argument("--workspace", required=True)
    for command in ("run", "stop"):
        child = sub.add_parser(command)
        child.add_argument("--workspace", required=True)
        child.add_argument("--timeout", type=float, default=900,
                           help="total execution timeout in seconds (default: 900)")
        if command == "run":
            child.add_argument("--retry", action="store_true",
                               help="explicitly repeat every lifecycle stage in an existing workspace")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            template = validate_template(read_json(args.template))
            if bool(args.catalog) != bool(args.request):
                fail("validate: --catalog and --request must be supplied together")
            if args.catalog:
                validate_request(template, read_json(args.request), validate_catalog(read_json(args.catalog)))
            print(json.dumps({"valid": True, "template_sha256": template_hash(template)}))
        elif args.command == "prepare":
            snapshot = prepare(read_json(args.template), read_json(args.catalog), read_json(args.request), args.workspace)
            print(json.dumps({"snapshot": str(snapshot)}))
        elif args.command == "replay":
            snapshot = replay(read_json(args.snapshot), read_json(args.catalog), args.workspace)
            print(json.dumps({"snapshot": str(snapshot)}))
        else:
            from . import runtime
            if args.command == "run":
                state = runtime.run(args.workspace, retry=args.retry, timeout=args.timeout)
            else:
                state = runtime.stop(args.workspace, timeout=args.timeout)
            print(json.dumps({"status": state["status"], "project_name": state["project_name"],
                              "execution": str(Path(args.workspace).resolve() / "execution.json"),
                              "output_dir": str(Path(args.workspace).resolve() / state["attempts"][-1]["output_dir"]),
                              "output_bindings": state.get("output_bindings", {})}))
        return 0
    except (StandError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"stand: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

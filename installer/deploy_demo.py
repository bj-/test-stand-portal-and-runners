"""Подготовка и запуск учебного шаблона в отдельном каталоге установки."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stands.stand import StandError, prepare, read_json, ref_name

FIXTURE = ROOT / "examples" / "template-demo"
FILES = ("app.py", "tasks.py", "smoke.py", "version.json", "Dockerfile",
         "Dockerfile.report", "report.mjs", "allurerc.json", "compose.yaml")
MARKER = ".template-demo-v1"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(repo, *args):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                             "-c", "commit.gpgsign=false", "-c", "user.name=Template Demo",
                             "-c", "user.email=demo@localhost", "-C", str(repo), *args],
                            env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise StandError("demo Git: " + result.stderr.strip())
    return result.stdout.strip()


def prepare_demo(workspace, api_ref=None, runner_ref=None):
    target = Path(os.path.abspath(workspace))
    for candidate in (target, *target.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise StandError("demo workspace must not traverse symlinks/junctions")
    resolved = target.resolve()
    if resolved == ROOT or ROOT in resolved.parents or resolved in ROOT.parents:
        raise StandError("demo workspace must be outside the source checkout")
    for value in (api_ref, runner_ref):
        if value is not None:
            ref_name(value)
    if target.exists() and any(target.iterdir()):
        if not (target / MARKER).is_file():
            raise StandError("demo workspace is not empty and has no installation marker")
    else:
        target.mkdir(parents=True, exist_ok=True)
        (target / MARKER).write_text("template-demo-v1\n", encoding="utf-8")

    repo = target / "source"
    if not repo.exists():
        repo.mkdir()
        for name in FILES:
            (repo / name).write_bytes((FIXTURE / name).read_bytes())
        git(repo, "init", "--initial-branch=main", "--template=")
        git(repo, "add", "--", *FILES)
        git(repo, "commit", "-m", "Учебный стенд: test")
        git(repo, "branch", "test")
        git(repo, "checkout", "-b", "feature-demo")
        write_json(repo / "version.json", {"version": "feature-demo"})
        git(repo, "add", "--", "version.json")
        git(repo, "commit", "-m", "Учебный стенд: feature-demo")
        git(repo, "checkout", "test")
    else:
        # Existing installations never have source files/branches rewritten.
        if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
            raise StandError("demo source is not its own Git repository; use a new workspace")
        git(repo, "rev-parse", "--verify", "refs/heads/test")
        git(repo, "rev-parse", "--verify", "refs/heads/feature-demo")

    request = {"schema_version": 1, "profile": "laptop", "components": {
        "api": {"ref": api_ref or "test"}, "smoke": {"ref": runner_ref or "test"}}, "parameters": {}}
    snapshot_path = target / "build" / "snapshot.json"
    if snapshot_path.exists():
        snapshot = read_json(snapshot_path)
        previous = {item["id"]: item["requested_ref"] for item in snapshot["components"]}
        for cid, requested in (("api", api_ref), ("smoke", runner_ref)):
            if requested is not None and requested != previous[cid]:
                raise StandError("existing snapshot uses different refs; choose a new workspace")
        return snapshot_path
    catalog = {"schema_version": 1, "repositories": [
        {"id": "demo", "path": str(repo.resolve()), "components": ["api", "smoke"]}]}
    template = read_json(FIXTURE / "template.json")
    write_json(target / "catalog.json", catalog)
    write_json(target / "request.json", request)
    write_json(target / "template.json", template)
    return prepare(template, catalog, request, target / "build")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--api-ref")
    parser.add_argument("--runner-ref")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        snapshot = prepare_demo(args.workspace, args.api_ref, args.runner_ref)
        print("Snapshot: " + str(snapshot), flush=True)
        if not args.prepare_only:
            from stands.runtime import run
            result = run(snapshot.parent, retry=args.retry, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (StandError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("deploy-demo: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

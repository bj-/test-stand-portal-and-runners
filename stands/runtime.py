"""Bounded local Docker Compose lifecycle for a prepared stand (stdlib only)."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
from ipaddress import IPv4Address, IPv4Network
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from . import stand


PUBLISH_NETWORKS = tuple(map(IPv4Network, ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def publish_address(value):
    try:
        address = IPv4Address(stand.string(value, "publish_ip"))
    except ValueError:
        stand.fail("runtime: publish_ip must be a private or loopback IPv4 address")
    if not any(address in network for network in PUBLISH_NETWORKS):
        stand.fail("runtime: publish_ip must be a private or loopback IPv4 address")
    return str(address)


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_path(workspace, path, *, exists=True):
    path = Path(path).absolute()
    if not stand.contains(workspace, path):
        stand.fail("runtime: path outside workspace")
    for part in (path, *path.parents):
        if stand.is_link(part):
            stand.fail("runtime: symlink/junction is forbidden")
    resolved = path.resolve()
    if not stand.contains(workspace, resolved) or (exists and not resolved.exists()):
        stand.fail("runtime: missing or outside-workspace path")
    return resolved


def load(workspace):
    raw = Path(workspace).absolute()
    for part in (raw, *raw.parents):
        if stand.is_link(part):
            stand.fail("runtime: symlink/junction in workspace path")
    root = raw.resolve()
    if stand.contains(stand.SOURCE_ROOT, root) or stand.contains(root, stand.SOURCE_ROOT):
        stand.fail("runtime: workspace must be outside tool repository")
    marker = safe_path(root, root / stand.MARKER)
    if marker.read_text(encoding="utf-8") != "stand-build-v1\n":
        stand.fail("runtime: invalid prepared workspace marker")
    snapshot = stand.read_json(safe_path(root, root / "snapshot.json"))
    # validate_snapshot needs compatibility metadata, not clones or Git objects.
    # Reconstruct only that metadata from the immutable snapshot for validation.
    repositories = {}
    if isinstance(snapshot, dict) and isinstance(snapshot.get("components"), list):
        for component in snapshot["components"]:
            if not isinstance(component, dict):
                stand.fail("runtime: invalid snapshot component")
            rid = stand.identifier(component.get("repository"), "snapshot repository")
            repositories.setdefault(rid, {"components": []})["components"].append(component.get("id"))
    template = stand.validate_snapshot(snapshot, repositories)
    if snapshot["secret_refs"]:
        stand.fail("runtime: secret_refs are not supported; no secrets are resolved")
    for component in template["components"]:
        context = safe_path(root, root / "contexts" / component["id"])
        if not context.is_dir():
            stand.fail("runtime: context must be a directory")
        # Prepared exports contain no links; reject subsequent accidental edits.
        for parent, dirs, files in os.walk(context, followlinks=False):
            for name in dirs + files:
                safe_path(root, Path(parent) / name)
    compose = template["compose"]
    compose_path = safe_path(root, root / "contexts" / compose["component"] / compose["path"])
    if not compose_path.is_file():
        stand.fail("runtime: compose file missing")
    return root, snapshot, compose_path


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def locked(root):
    path = safe_path(root, root / ".execution.lock", exists=False)
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError:
        stand.fail("runtime: workspace is locked; inspect the owner before manually removing a stale .execution.lock")
    try:
        with stream:
            json.dump({"pid": os.getpid(), "created_at": now()}, stream)
            stream.flush()
        yield
    finally:
        path.unlink()


def environment(root, snapshot, output_dir=None):
    keys = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "DOCKER_HOST", "DOCKER_CONTEXT",
            "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"}
    allowed = {key.upper() for key in keys}
    env = {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(OUTPUT_DIR=str(output_dir or root / "results"), COMPOSE_DISABLE_ENV_FILE="1", COMPOSE_ANSI="never")
    generated = set()
    for prefix, values in (("CONTEXT_", {c["id"]: str(root / "contexts" / c["id"]) for c in snapshot["components"]}),
                           ("STAND_PARAM_", snapshot["parameters"])):
        for name, value in values.items():
            key = prefix + re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
            if key in generated:
                stand.fail("runtime: parameter/component environment names collide after normalization")
            generated.add(key)
            env[key] = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
    return env


def check_binding_names(template, env):
    controlled = {name.upper() for name in env}
    seen = set()
    for name in template["output_bindings"]:
        normalized = name.upper()
        if normalized in seen:
            stand.fail("runtime: output binding names collide ignoring case")
        seen.add(normalized)
        if normalized in controlled or normalized.startswith(("DOCKER_", "COMPOSE_", "CONTEXT_", "STAND_PARAM_")):
            stand.fail("runtime: output binding collides with controlled environment")
    if "BASEURL" in seen and "BASE_URL" in seen:
        stand.fail("runtime: baseUrl and BASE_URL binding aliases collide")


class CommandFailed(stand.StandError):
    """A command completed with a nonzero exit status."""


class Cancelled(stand.StandError):
    """The owner requested cancellation; containers and data are retained."""


def terminate_command(process, *, process_group=False):
    if os.name != "nt" and process_group:
        # Managed Compose calls own a session, so plugins cannot outlive an
        # interrupted deletion and race a subsequent retry on the same stand.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        except ProcessLookupError:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=3)
        return
    if process.poll() is not None:
        return
    if os.name == "nt":
        # docker.exe starts Compose and Buildx as plugins. Stopping only the
        # CLI can leave an in-flight BuildKit operation shared by the next build.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


class Executor:
    def __init__(self, root, env, timeout, cancel=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            stand.fail("runtime: timeout must be a positive finite number of seconds")
        self.root, self.env = root, env
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel

    def checkpoint(self):
        if self.cancel and self.cancel():
            raise Cancelled("runtime: cancelled; containers/data retained")

    def call(self, argv, log, *, capture=False, capture_stderr=False):
        self.checkpoint()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            stand.fail("runtime: lifecycle timeout exceeded")
        with log.open("a", encoding="utf-8", errors="replace") as stream:
            stream.write(f"\n[{now()}] {argv[0]} {argv[1] if len(argv) > 1 else ''}\n")
            stream.flush()
            try:
                if self.cancel is not None:
                    # A file avoids PIPE deadlocks while polling cancellation, even
                    # when Compose produces more output than an OS pipe can hold.
                    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
                        process = subprocess.Popen(argv, cwd=self.root, env=self.env, shell=False,
                            stdin=subprocess.DEVNULL, stdout=output if capture else stream,
                            stderr=output if capture_stderr else stream, text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
                        try:
                            while process.poll() is None:
                                self.checkpoint()
                                if time.monotonic() >= self.deadline:
                                    raise subprocess.TimeoutExpired(argv, remaining)
                                time.sleep(0.1)
                            self.checkpoint()
                        except BaseException:
                            terminate_command(process, process_group=os.name != "nt")
                            raise
                        output.seek(0)
                        result = subprocess.CompletedProcess(argv, process.returncode, output.read() if capture else "")
                else:
                    result = subprocess.run(argv, cwd=self.root, env=self.env, shell=False,
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE if capture else stream,
                                        stderr=subprocess.STDOUT if capture_stderr else stream, text=True, encoding="utf-8", errors="replace",
                                        timeout=remaining)
            except subprocess.TimeoutExpired:
                stream.write("runtime: command timed out; containers/data retained\n")
                stand.fail("runtime: command timed out; inspect stage logs (containers/data retained)")
            except OSError:
                stream.write("runtime: command could not start\n")
                stand.fail("runtime: Docker command could not start; inspect stage logs")
            if result.returncode:
                stream.write(f"runtime: exit {result.returncode}\n")
                raise CommandFailed(f"runtime: command failed (exit {result.returncode}); inspect stage logs")
            return result.stdout or ""

    def docker_check(self, log):
        endpoint = self.env.get("DOCKER_HOST") if not self.env.get("DOCKER_CONTEXT") else None
        if not endpoint:
            context = [self.env["DOCKER_CONTEXT"]] if self.env.get("DOCKER_CONTEXT") else []
            endpoint = self.call(["docker", "context", "inspect", *context, "--format", "{{.Endpoints.docker.Host}}"], log, capture=True).strip()
        if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
            stand.fail("runtime: local Docker context required; remote daemon rejected")
        self.call(["docker", "compose", "version"], log, capture=True)
        if self.call(["docker", "info", "--format", "{{.OSType}}"], log, capture=True).strip() != "linux":
            stand.fail("runtime: Docker daemon must use Linux containers")


def json_output(text, label):
    # Use the strict reader without leaving config/environment values on disk.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                stand.fail(f"runtime: duplicate JSON key in {label}")
            result[key] = value
        return result
    def constant(_):
        stand.fail(f"runtime: nonfinite JSON in {label}")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, TypeError):
        stand.fail(f"runtime: invalid JSON from {label}")


def prefix(root, compose_path, project, override=None):
    argv = ["docker", "compose", "--project-name", project, "--project-directory", str(compose_path.parent),
            "--env-file", str(root / ".runtime.env"), "--profile", "*", "--file", str(compose_path)]
    if override:
        argv.extend(["--file", str(override)])
    return argv


def validate_config(config, root, selected, project, *, fixed=None, publish_ip="127.0.0.1"):
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        stand.fail("runtime: Compose config must contain services")
    services = config["services"]
    if not set(selected).issubset(services):
        stand.fail("runtime: selected/required service missing from Compose config")
    for kind in ("volumes", "networks"):
        for key, value in (config.get(kind) or {}).items():
            value = value or {}
            if value.get("external") or (value.get("name") and value["name"] != f"{project}_{key}"):
                stand.fail(f"runtime: external or fixed-name {kind} are forbidden")
            if kind == "volumes" and (value.get("driver_opts") or value.get("driver", "local") != "local"):
                stand.fail("runtime: volume drivers/options can escape workspace")
    for name, service in services.items():
        if name not in selected:
            # A dependency cannot silently start a service without resource limits.
            continue
        if (service.get("container_name") or service.get("privileged") or service.get("network_mode")
                or service.get("pid") or service.get("ipc") or service.get("devices")
                or service.get("volumes_from") or service.get("cap_add") or service.get("use_api_socket")
                or service.get("develop") or service.get("provider")):
            stand.fail("runtime: service uses unsupported host/shared-container capabilities")
        dependencies = service.get("depends_on") or {}
        if not set(dependencies).issubset(selected):
            stand.fail("runtime: service dependency is outside selected profile")
        if service.get("scale", 1) != 1 or (service.get("deploy") or {}).get("replicas", 1) != 1:
            stand.fail("runtime: multiple service replicas are not supported")
        for network in (service.get("networks") or {}):
            if network not in (config.get("networks") or {}):
                stand.fail("runtime: unknown network")
        for port in service.get("ports") or []:
            if (not isinstance(port, dict) or port.get("host_ip") != publish_ip
                    or (str(port.get("published", "")) not in ("", "0") if fixed is None else
                        (name, port.get("target"), port.get("protocol", "tcp"), str(port.get("published", ""))) not in fixed)
                    or port.get("mode", "ingress") != "ingress"):
                stand.fail("runtime: ports must use dynamic 127.0.0.1 bindings")
        for mount in service.get("volumes") or []:
            if not isinstance(mount, dict):
                stand.fail("runtime: unresolved volume definition")
            if mount.get("type") == "bind":
                safe_path(root, mount.get("source", ""), exists=False)
            elif mount.get("type") == "volume":
                if mount.get("source") and mount["source"] not in (config.get("volumes") or {}):
                    stand.fail("runtime: unknown named volume")
            elif mount.get("type") != "tmpfs":
                stand.fail("runtime: unsupported mount type")
        build = service.get("build")
        if build:
            if not isinstance(build, dict) or not build.get("context"):
                stand.fail("runtime: local build context required")
            context = safe_path(root, build["context"])
            if build.get("dockerfile"):
                safe_path(root, context / build["dockerfile"])
            if (build.get("additional_contexts") or build.get("ssh") or build.get("secrets")
                    or build.get("entitlements") or build.get("network") == "host"):
                stand.fail("runtime: unsupported external/privileged build inputs")
        for env_file in service.get("env_file") or []:
            safe_path(root, env_file["path"] if isinstance(env_file, dict) else env_file)
        if service.get("configs") or service.get("secrets"):
            stand.fail("runtime: Compose configs/secrets are not supported")
    return services


def reserved_ports(template, ports):
    if not isinstance(ports, dict) or set(ports) != set(template["output_bindings"]):
        stand.fail("runtime: reserved ports must exactly match output bindings")
    groups, owners = {}, {}
    for name, binding in template["output_bindings"].items():
        port = ports[name]
        if type(port) is not int or not 1 <= port <= 65535:
            stand.fail("runtime: reserved port must be integer 1..65535")
        group = (binding["service"], binding["port"])
        if group in groups and groups[group] != port:
            stand.fail("runtime: binding aliases require the same reserved port")
        if port in owners and owners[port] != group:
            stand.fail("runtime: distinct bindings cannot share a reserved port")
        groups[group], owners[port] = port, group
    return groups


def effective_config(config, root, selected, project, template, ports, resources, *, publish_ip="127.0.0.1"):
    # Validate the untrusted, dynamic-only source BEFORE introducing fixed ports.
    validate_config(config, root, selected, project)
    groups = reserved_ports(template, ports)
    value = copy.deepcopy(config)
    expected = {(service, target, "tcp", str(port)) for (service, target), port in groups.items()}
    for name in selected:
        service = value["services"][name]
        service["ports"] = [{"target": target, "published": str(port), "host_ip": publish_ip, "protocol": "tcp", "mode": "ingress"}
                            for (owner, target), port in groups.items() if owner == name]
        service.update(cpus=resources["cpus"], mem_limit=f"{resources['memory_mb']}m")
        service.setdefault("deploy", {}).setdefault("resources", {})["limits"] = {
            "cpus": str(resources["cpus"]), "memory": f"{resources['memory_mb']}m"}
    # Unselected services must not retain publishing either.
    for name, service in value["services"].items():
        if name not in selected:
            service.pop("ports", None)
    validate_config(value, root, selected, project, fixed=expected, publish_ip=publish_ip)
    return value


def normalize(command, stage, selected):
    argv = command["argv"]
    if argv[:2] != ["docker", "compose"] or len(argv) < 3:
        stand.fail("runtime: lifecycle supports only docker compose argv; host commands/global overrides are forbidden")
    operation, args = argv[2], argv[3:]
    expected = {"build": {"build"}, "deploy": {"up"}, "migrate": {"run"}, "seed": {"run"},
                "healthcheck": {"up", "run"}, "smoke": {"run"}, "liveness": {"run"}}[stage]
    if operation not in expected:
        stand.fail(f"runtime: unsupported Compose operation for {stage}")
    # Parse only a deliberately small, explicit option set. After run SERVICE,
    # every remaining token is a container command, never a host CLI option.
    switches = {"build": {"--no-cache", "--pull"},
                "up": {"-d", "--detach", "--wait", "--no-deps", "--build", "--no-build"},
                "run": {"--rm", "--no-deps", "--build", "-T"}}[operation]
    if stage == "liveness":
        switches = {"--rm", "--no-deps", "-T"}
    values = {"--wait-timeout"} if operation == "up" else set()
    options, targets = [], []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("-"):
            if arg in switches:
                options.append(arg)
            elif arg in values and index + 1 < len(args) and re.fullmatch(r"[1-9][0-9]*", args[index + 1]):
                options.extend(args[index:index + 2])
                index += 1
            else:
                stand.fail("runtime: unsupported Compose option (project/file/env overrides are forbidden)")
        else:
            if arg not in selected:
                stand.fail("runtime: command service is outside selected profile")
            targets.append(arg)
            if operation == "run":
                tail = args[index + 1:]
                if "--rm" not in options:
                    options.append("--rm")
                if stage == "liveness" and "--no-deps" not in options:
                    options.append("--no-deps")
                return [operation, *options, arg, *tail]
        index += 1
    if operation == "run":
        stand.fail("runtime: compose run requires a selected service")
    if stage == "healthcheck" and "--wait" not in options:
        stand.fail("runtime: healthcheck up must use --wait; ps is not a readiness check")
    if operation == "up" and not ({"-d", "--detach", "--wait"} & set(options)):
        stand.fail("runtime: compose up must be detached or --wait")
    return [operation, *options, *(targets or selected)]


def existing_state(root, snapshot):
    path = safe_path(root, root / "execution.json", exists=False)
    if not path.exists():
        return None
    state = stand.read_json(path)
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or not re.fullmatch(r"stand-[a-f0-9]{32}", str(state.get("project_name", "")))
            or state.get("workspace") != str(root)
            or state.get("snapshot_sha256") != hashlib.sha256(stand.canonical(snapshot)).hexdigest()
            or not isinstance(state.get("attempts"), list)):
        stand.fail("runtime: invalid or changed execution identity/snapshot")
    return state


def binding_urls(executor, compose, template, log, ports=None, *, publish_ip="127.0.0.1"):
    urls = {}
    for name, binding in template["output_bindings"].items():
        value = executor.call([*compose, "port", binding["service"], str(binding["port"])], log, capture=True).strip()
        match = re.fullmatch(re.escape(publish_ip) + r":([0-9]+)", value)
        if not match or not 1 <= int(match[1]) <= 65535:
            stand.fail("runtime: output binding did not resolve to one port on the configured address")
        if ports is not None and int(match[1]) != ports[name]:
            stand.fail("runtime: Docker published port differs from reserved port")
        urls[name] = f"{binding['scheme']}://{value}{binding['path']}"
    return urls


def run(workspace, retry=False, timeout=900, *, ports=None, progress=None, cancel=None, publish_ip="127.0.0.1",
        test_data=None, data_context=None, managed_run_overlays=None):
    publish_ip = publish_address(publish_ip)
    if ports is None and publish_ip != "127.0.0.1":
        stand.fail("runtime: publishing beyond the default loopback requires reserved ports")
    root, snapshot, compose_path = load(workspace)
    entries = stand.validate_test_data(snapshot["template"], snapshot["profile"], [] if test_data is None else test_data)
    if entries:
        if not isinstance(data_context, dict) or set(data_context) != {"project", "stand", "target_host"}:
            stand.fail("runtime: data adapters require worker identity")
        from . import data_modules
        for entry in entries:
            module = snapshot["template"]["data_modules"][entry["module"]]
            if "run_binding" in module:
                if managed_run_overlays is None:
                    stand.fail("runtime: run_binding requires configured managed_run_overlays")
                data_modules.overlay_path(managed_run_overlays, data_context["project"], data_context["stand"], module["run_binding"])
    with locked(root):
        state = existing_state(root, snapshot)
        if state is not None and not retry:
            stand.fail("runtime: workspace has a prior execution; use --retry explicitly (migration/seed repeat)")
        if state is not None and state.get("reserved_ports") != ports:
            stand.fail("runtime: reserved ports differ from persisted execution identity")
        if state is not None and state.get("publish_ip", "127.0.0.1") != publish_ip:
            stand.fail("runtime: publication address differs from persisted execution identity")
        if state is None:
            state = {"schema_version": 1, "project_name": "stand-" + uuid.uuid4().hex, "workspace": str(root),
                     "snapshot_sha256": hashlib.sha256(stand.canonical(snapshot)).hexdigest(),
                     "template_sha256": snapshot["template_sha256"], "sources": snapshot["components"],
                     "profile": snapshot["profile"], "attempts": []}
            if ports is not None:
                reserved_ports(snapshot["template"], ports)
                state["reserved_ports"] = dict(ports)
            if publish_ip != "127.0.0.1":
                state["publish_ip"] = publish_ip
        attempt_id = uuid.uuid4().hex
        logs = safe_path(root, root / "logs" / attempt_id, exists=False)
        logs.mkdir(parents=True)
        output_dir = safe_path(root, root / "results" / attempt_id, exists=False)
        output_dir.mkdir(parents=True)
        attempt = {"id": attempt_id, "started_at": now(), "status": "running", "stages": [],
                   "logs": str(logs.relative_to(root)), "output_dir": str(output_dir.relative_to(root))}
        state["attempts"].append(attempt)
        state["status"] = "running"
        state.pop("output_bindings", None)
        state.pop("images", None)
        def save():
            atomic_json(root / "execution.json", state)
        save()
        try:
            print(f"stand: preflight; logs {logs}", file=sys.stderr, flush=True)
            template = snapshot["template"]
            selected = template["profiles"][snapshot["profile"]]["services"]
            commands = {stage: [normalize(c, stage, selected) for c in template["lifecycle"][stage]] for stage in stand.STAGES}
            probes = {name: normalize(command, "liveness", selected)
                      for name, command in template.get("liveness", {}).items() if name in selected}
            executor = Executor(root, environment(root, snapshot, output_dir), timeout, cancel=cancel)
            check_binding_names(template, executor.env)
            log = logs / "preflight.log"
            executor.docker_check(log)
            env_file = safe_path(root, root / ".runtime.env", exists=False)
            env_file.write_text("", encoding="utf-8")
            compose = prefix(root, compose_path, state["project_name"])
            config = json_output(executor.call([*compose, "config", "--format", "json", "--no-env-resolution"], log, capture=True), "Compose config")
            services = validate_config(config, root, selected, state["project_name"])
            resources = template["profiles"][snapshot["profile"]]["resources"]
            override = safe_path(root, root / "runtime.override.json", exists=False)
            atomic_json(override, {"services": {name: {"cpus": resources["cpus"], "mem_limit": f"{resources['memory_mb']}m",
                         "deploy": {"resources": {"limits": {"cpus": str(resources["cpus"]), "memory": f"{resources['memory_mb']}m"}}}}
                         for name in selected}})
            compose = prefix(root, compose_path, state["project_name"], override)
            if ports is not None:
                effective = effective_config(config, root, selected, state["project_name"], template, ports, resources,
                                             publish_ip=publish_ip)
                effective_path = safe_path(root, root / "runtime.effective.json", exists=False)
                atomic_json(effective_path, effective)
                state["effective_sha256"] = hashlib.sha256(stand.canonical(effective)).hexdigest()
                save()
                compose = prefix(root, effective_path, state["project_name"])
            limited = False
            for stage in stand.STAGE_ORDER:
                if stage == "liveness" and not limited:
                    continue
                executor.checkpoint()
                print(f"stand: {stage} started", file=sys.stderr, flush=True)
                record = {"name": stage, "status": "running", "started_at": now(), "log": str((logs / (stage + ".log")).relative_to(root))}
                attempt["stages"].append(record)
                save()
                if progress:
                    progress(dict(record))
                failures = []
                stage_commands = probes.items() if stage == "liveness" else ((None, argv) for argv in commands[stage])
                for name, argv in stage_commands:
                    if name is not None:
                        with (logs / "liveness.log").open("a", encoding="utf-8") as stream:
                            stream.write(f"\n[{now()}] liveness: {name}\n")
                    try:
                        executor.call([*compose, *argv], logs / (stage + ".log"))
                    except CommandFailed as exc:
                        if stage == "smoke" and probes:
                            limited = True
                            failures.append(str(exc))
                            break
                        if stage == "liveness":
                            failures.append(f"{name}: {exc}")
                        else:
                            raise
                if stage == "liveness" and failures:
                    raise CommandFailed("runtime: liveness failed: " + "; ".join(failures))
                if stage == "seed" and entries:
                    pending = data_modules.apply(executor, compose, snapshot, entries, project=data_context["project"],
                                                 stand_id=data_context["stand"], managed_root=managed_run_overlays)
                    executor.checkpoint()
                    data_modules.publish(managed_run_overlays, pending, project=data_context["project"],
                                         stand_id=data_context["stand"], target_host=data_context["target_host"], workspace=root)
                    with (logs / "seed.log").open("a", encoding="utf-8") as stream:
                        for entry in entries:
                            stream.write("data module: " + entry["module"] + " success\n")
                if stage == "deploy":
                    bindings = binding_urls(executor, compose, template, logs / "deploy.log", ports, publish_ip=publish_ip)
                    attempt["output_bindings"] = state["output_bindings"] = bindings
                    atomic_json(safe_path(root, output_dir / "output-bindings.json", exists=False), bindings)
                    executor.env.update(bindings)
                    if "baseUrl" in bindings:
                        executor.env["BASE_URL"] = bindings["baseUrl"]
                record.update(status="failed" if failures else "success", finished_at=now())
                if failures:
                    record["error"] = "; ".join(failures)
                save()
                if progress:
                    progress(dict(record))
                print(f"stand: {stage} {record['status']}", file=sys.stderr, flush=True)
            refs = list(dict.fromkeys(services[name].get("image") or f"{state['project_name']}-{name}" for name in selected))
            images = json_output(executor.call(["docker", "image", "inspect", *refs], logs / "images.log", capture=True), "image inspect")
            if not isinstance(images, list) or len(images) != len(refs) or any(not isinstance(i, dict) or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(i.get("Id", ""))) for i in images):
                stand.fail("runtime: image inspect did not provide immutable image IDs")
            attempt["images"] = state["images"] = [{"id": i["Id"], "repo_digests": i.get("RepoDigests") or [], "repo_tags": i.get("RepoTags") or []} for i in images]
            outcome = "limited" if limited else "success"
            attempt.update(status=outcome, finished_at=now())
            state["status"] = outcome
            save()
            print(f"stand: {outcome}; results {output_dir}", file=sys.stderr, flush=True)
            for name, url in state.get("output_bindings", {}).items():
                print(f"stand: {name}={url}", file=sys.stderr, flush=True)
            return state
        except (Exception, KeyboardInterrupt) as exc:
            attempt.update(status="failed", finished_at=now(), error=str(exc))
            for record in attempt["stages"]:
                if record["status"] == "running":
                    record.update(status="failed", finished_at=now())
                    if progress:
                        try:
                            progress(dict(record))
                        except Exception:
                            pass
            state["status"] = "failed"
            save()
            print(f"stand: failed; logs {logs}; containers/data retained", file=sys.stderr, flush=True)
            raise


def stop(workspace, timeout=900):
    root, snapshot, compose_path = load(workspace)
    with locked(root):
        state = existing_state(root, snapshot)
        if state is None:
            stand.fail("runtime: workspace has no execution to stop")
        override = safe_path(root, root / "runtime.override.json")
        if safe_path(root, root / ".runtime.env").read_text(encoding="utf-8"):
            stand.fail("runtime: controlled environment file was modified")
        log = safe_path(root, root / "logs" / ("stop-" + uuid.uuid4().hex + ".log"), exists=False)
        log.parent.mkdir(exist_ok=True)
        record = {"started_at": now(), "status": "running", "log": str(log.relative_to(root))}
        state.setdefault("stops", []).append(record)
        atomic_json(root / "execution.json", state)
        try:
            output_dir = safe_path(root, root / state["attempts"][-1]["output_dir"])
            env = environment(root, snapshot, output_dir)
            env.update(state.get("output_bindings", {}))
            if "baseUrl" in env:
                env["BASE_URL"] = env["baseUrl"]
            executor = Executor(root, env, timeout)
            executor.docker_check(log)
            fixed = None
            publish_ip = publish_address(state.get("publish_ip", "127.0.0.1"))
            if "reserved_ports" in state:
                groups = reserved_ports(snapshot["template"], state["reserved_ports"])
                fixed = {(service, target, "tcp", str(port)) for (service, target), port in groups.items()}
                effective_path = safe_path(root, root / "runtime.effective.json")
                effective = stand.read_json(effective_path)
                if hashlib.sha256(stand.canonical(effective)).hexdigest() != state.get("effective_sha256"):
                    stand.fail("runtime: changed effective config identity")
                validate_config(effective, root, snapshot["template"]["profiles"][snapshot["profile"]]["services"],
                                state["project_name"], fixed=fixed, publish_ip=publish_ip)
                compose = prefix(root, effective_path, state["project_name"])
            else:
                compose = prefix(root, compose_path, state["project_name"], override)
            config = json_output(executor.call([*compose, "config", "--format", "json", "--no-env-resolution"], log, capture=True), "Compose config")
            selected = snapshot["template"]["profiles"][snapshot["profile"]]["services"]
            validate_config(config, root, selected, state["project_name"], fixed=fixed, publish_ip=publish_ip)
            executor.call([*compose, "stop", *selected], log)
            record.update(status="success", finished_at=now())
            state["status"] = "stopped"
            atomic_json(root / "execution.json", state)
            print("stand: stopped; containers/data retained", file=sys.stderr, flush=True)
            return state
        except (Exception, KeyboardInterrupt) as exc:
            record.update(status="failed", finished_at=now(), error=str(exc))
            atomic_json(root / "execution.json", state)
            raise


def deletion_resources(executor, config, compose, project, log):
    """Refuse same-name foreign resources before Compose can remove anything."""
    label = "com.docker.compose.project"
    ids = executor.call(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={label}={project}"], log, capture=True).split()
    containers = json_output(executor.call(["docker", "container", "inspect", *ids], log, capture=True), "container inspect") if ids else []
    if (not isinstance(containers, list) or len(containers) != len(ids)
            or {container.get("Id") for container in containers} != set(ids)):
        stand.fail("runtime: incomplete container ownership inspection")
    files = [str(Path(compose[index + 1]).resolve()) for index, arg in enumerate(compose[:-1]) if arg == "--file"]
    directory = str(Path(compose[compose.index("--project-directory") + 1]).resolve())
    volume_keys = {f"{project}_{key}": key for key in config.get("volumes", {})}
    used_volumes = set(volume_keys)
    for container in containers:
        labels = (container.get("Config") or {}).get("Labels") or {}
        actual_files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if (labels.get(label) != project or not labels.get("com.docker.compose.service")
                or [str(Path(path).resolve()) for path in actual_files] != files
                or str(Path(labels.get("com.docker.compose.project.working_dir", "")).resolve()) != directory):
            stand.fail("runtime: container ownership/configuration path mismatch")
        for mount in container.get("Mounts") or []:
            if mount.get("Type") == "volume":
                name = mount.get("Name", "")
                if name not in volume_keys and not re.fullmatch(r"[a-f0-9]{64}", name):
                    stand.fail("runtime: container mounts a foreign named volume")
                used_volumes.add(name)
    # List first: an absent volume/network is a normal idempotent retry, whereas
    # an inspect error must never be interpreted as proof that it is absent.
    names = executor.call(["docker", "volume", "ls", "--format", "{{.Name}}"], log, capture=True).splitlines()
    for name in sorted(used_volumes.intersection(names)):
        values = json_output(executor.call(["docker", "volume", "inspect", name], log, capture=True), "volume inspect")
        if not isinstance(values, list) or len(values) != 1 or values[0].get("Name") != name:
            stand.fail("runtime: invalid volume ownership inspection")
        labels = values[0].get("Labels") or {}
        if name in volume_keys and (labels.get(label) != project or labels.get("com.docker.compose.volume") != volume_keys[name]):
            stand.fail("runtime: foreign named volume ownership")
        if name not in volume_keys and ("com.docker.volume.anonymous" not in labels
                                       or labels.get(label) not in (None, project)):
            stand.fail("runtime: unverified anonymous volume ownership")
        users = executor.call(["docker", "ps", "-aq", "--no-trunc", "--filter", "volume=" + name], log, capture=True).split()
        if not set(users).issubset(ids):
            stand.fail("runtime: volume is shared with another project")
    networks = executor.call(["docker", "network", "ls", "--format", "{{.Name}}"], log, capture=True).splitlines()
    for key in config.get("networks", {}):
        name = f"{project}_{key}"
        if name not in networks:
            continue
        values = json_output(executor.call(["docker", "network", "inspect", name], log, capture=True), "network inspect")
        if not isinstance(values, list) or len(values) != 1 or values[0].get("Name") != name:
            stand.fail("runtime: invalid network ownership inspection")
        labels = values[0].get("Labels") or {}
        if labels.get(label) != project or labels.get("com.docker.compose.network") != key:
            stand.fail("runtime: foreign network ownership")
        if not set(values[0].get("Containers") or {}).issubset(ids):
            stand.fail("runtime: network is shared with another project")
    return containers


def restart_targets(template, config, selected):
    services = config["services"]
    deployed = set()
    initializers = set()
    for command in template["lifecycle"]["deploy"]:
        argv = normalize(command, "deploy", selected)
        targets = set(argv) & set(selected)
        if "--no-deps" not in argv:
            pending = list(targets)
            while pending:
                name = pending.pop()
                for dependency, options in (services[name].get("depends_on") or {}).items():
                    if options.get("condition") == "service_completed_successfully":
                        initializers.add(dependency)
                    if options.get("required", True) and dependency not in targets:
                        targets.add(dependency)
                        pending.append(dependency)
        deployed.update(targets)
    deployed.update(binding["service"] for binding in template["output_bindings"].values())
    return deployed - initializers, initializers


def control(workspace, *, stand_id, project, target_host, action, timeout=900, cancel=None):
    """Fixed, bounded operations on an inspected worker-owned deployment only."""
    stand.identifier(stand_id, "stand id")
    stand.identifier(target_host, "target host")
    if action not in ("restart", "status", "logs"):
        stand.fail("runtime: unsupported stand control action")
    root, snapshot, _ = load(workspace)
    with locked(root):
        identity = stand.read_json(safe_path(root, root / ".worker-identity.json"))
        expected = {"stand": stand_id, "project": project, "target_host": target_host,
                    "profile": snapshot["profile"], "template_sha256": snapshot["template_sha256"]}
        if isinstance(identity, dict):
            # Early worker-owned workspaces were already scoped to this host's root.
            identity.setdefault("target_host", target_host)
        if not isinstance(identity, dict) or any(identity.get(key) != value for key, value in expected.items()):
            stand.fail("runtime: stand control workspace ownership mismatch")
        state = existing_state(root, snapshot)
        if (state is None or state.get("status") == "deleted" or state.get("reserved_ports") != identity.get("ports")
                or state.get("template_sha256") != snapshot["template_sha256"] or state.get("profile") != snapshot["profile"]):
            stand.fail("runtime: stand control execution identity mismatch")
        groups = reserved_ports(snapshot["template"], state["reserved_ports"])
        fixed = {(service, target, "tcp", str(port)) for (service, target), port in groups.items()}
        effective_path = safe_path(root, root / "runtime.effective.json")
        effective = stand.read_json(effective_path)
        if hashlib.sha256(stand.canonical(effective)).hexdigest() != state.get("effective_sha256"):
            stand.fail("runtime: changed effective config identity")
        selected = snapshot["template"]["profiles"][snapshot["profile"]]["services"]
        address = publish_address(state.get("publish_ip", "127.0.0.1"))
        if identity.get("publish_ip", "127.0.0.1") != address:
            stand.fail("runtime: publication identity mismatch")
        validate_config(effective, root, selected, state["project_name"], fixed=fixed, publish_ip=address)
        if safe_path(root, root / ".runtime.env").read_text(encoding="utf-8"):
            stand.fail("runtime: controlled environment file was modified")
        # Logs from inspection contain no captured service output. Raw service
        # logs remain in memory and are redacted by the worker before upload.
        log = safe_path(root, root / "logs" / ("control-" + uuid.uuid4().hex + ".log"), exists=False)
        log.parent.mkdir(exist_ok=True)
        executor = Executor(root, environment(root, snapshot), timeout, cancel=cancel or (lambda: False))
        executor.docker_check(log)
        compose = prefix(root, effective_path, state["project_name"])
        containers = deletion_resources(executor, effective, compose, state["project_name"], log)
        for container in containers:
            labels = (container.get("Config") or {}).get("Labels") or {}
            if (labels.get("com.docker.compose.service") not in selected
                    or str(labels.get("com.docker.compose.oneoff", "false")).lower() != "false"
                    or not re.fullmatch(r"[a-f0-9]{64}", str(container.get("Id", "")))):
                stand.fail("runtime: stand control found an unexpected container")
        if action == "logs":
            return {"logs": [(container["Config"]["Labels"]["com.docker.compose.service"],
                              executor.call(["docker", "container", "logs", "--tail", "100", "--timestamps", container["Id"]],
                                            log, capture=True, capture_stderr=True)[-24000:]) for container in containers]}
        if action == "restart":
            executor.checkpoint()
            required, initializers = restart_targets(snapshot["template"], effective, selected)
            owned_services = {container["Id"]: container["Config"]["Labels"]["com.docker.compose.service"] for container in containers}
            ids = [ident for ident, service in owned_services.items() if service not in initializers]
            if not ids or not required.issubset(owned_services.values()):
                stand.fail("runtime: required deployed containers are missing")
            executor.call(["docker", "container", "restart", *ids], log, capture=True)
            while True:
                executor.checkpoint()
                if time.monotonic() >= executor.deadline:
                    stand.fail("runtime: restarted containers did not become ready")
                containers = json_output(executor.call(["docker", "container", "inspect", *ids], log, capture=True), "restart status")
                if (not isinstance(containers, list) or len(containers) != len(ids)
                        or {container.get("Id") for container in containers} != set(ids)
                        or any(((container.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service")
                               != owned_services.get(container.get("Id")) for container in containers)):
                    stand.fail("runtime: incomplete restart readiness inspection")
                if all((container.get("State") or {}).get("Status") == "running"
                       and ((container.get("State") or {}).get("Health") or {}).get("Status", "healthy") == "healthy"
                       for container in containers):
                    break
                time.sleep(0.1)
        projection = []
        for container in containers:
            labels = (container.get("Config") or {}).get("Labels") or {}
            status = (container.get("State") or {}).get("Status", "unknown")
            health = ((container.get("State") or {}).get("Health") or {}).get("Status", "none")
            if status not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
                status = "unknown"
            if health not in ("starting", "healthy", "unhealthy", "none"):
                health = "unknown"
            projection.append({"service": labels.get("com.docker.compose.service"), "state": status, "health": health})
        return {"services": projection}


def deletion_bind_paths(root, config, compose_path):
    paths = set()
    preserved = [root / name for name in ("results", "logs")]
    protected = [root / name for name in (stand.MARKER, "snapshot.json", "execution.json", ".worker-identity.json",
                                         ".execution.lock", ".runtime.env", "runtime.effective.json", "runtime.override.json")]
    protected.append(compose_path)
    for service in config["services"].values():
        if service.get("build"):
            context = Path(service["build"]["context"])
            protected.extend((context, context / service["build"].get("dockerfile", "Dockerfile")))
    for service in config["services"].values():
        for mount in service.get("volumes") or []:
            if mount.get("type") != "bind" or mount.get("read_only"):
                continue
            path = safe_path(root, mount["source"], exists=False)
            if any(stand.contains(keep, path) for keep in preserved):
                continue  # Build reports remain available after the stand is gone.
            if path == root or any(stand.contains(path, keep) for keep in preserved + protected):
                stand.fail("runtime: writable bind overlaps preserved workspace metadata")
            if path.is_dir():
                for parent, dirs, files in os.walk(path, followlinks=False):
                    for name in dirs + files:
                        safe_path(root, Path(parent) / name)
            paths.add(path)
    return sorted(paths, key=lambda path: len(path.parts))


def delete(workspace, *, stand_id, project, target_host, workspace_required=True, timeout=900, cancel=None):
    """Delete only a worker-owned stand's Compose resources and writable bind data.

    Keep snapshots, exported sources, logs and reports for audit/recovery. No image,
    global Docker prune, external volume or unrelated workspace is removed.
    """
    stand.identifier(stand_id, "stand id")
    stand.identifier(target_host, "target host")
    if type(workspace_required) is not bool:
        stand.fail("runtime: workspace_required must be boolean")
    root = safe_path(Path(workspace).absolute(), workspace, exists=False)
    if stand.contains(stand.SOURCE_ROOT, root) or stand.contains(root, stand.SOURCE_ROOT):
        stand.fail("runtime: workspace must be outside tool repository")
    if not root.exists():
        if workspace_required:
            stand.fail("runtime: acknowledged stand workspace is missing; deletion cannot be verified")
        return {"status": "deleted"}  # Portal can delete a stand before its first build.
    with locked(root):
        identity = stand.read_json(safe_path(root, root / ".worker-identity.json"))
        if (not isinstance(identity, dict) or identity.get("stand") != stand_id or identity.get("project") != project
                or identity.get("target_host", target_host) != target_host):
            stand.fail("runtime: stand deletion workspace ownership mismatch")
        root, snapshot, compose_path = load(root)
        if (identity.get("template_sha256") != snapshot["template_sha256"] or identity.get("profile") != snapshot["profile"]):
            stand.fail("runtime: stand deletion snapshot identity mismatch")
        state = existing_state(root, snapshot)
        if state is None:
            # Preparation alone never starts Docker or creates stand data.
            return {"status": "deleted"}
        if state.get("reserved_ports") != identity.get("ports"):
            stand.fail("runtime: stand deletion execution identity mismatch")
        if state.get("status") == "deleted":
            return state
        # Portal executions always persist this canonical configuration before
        # their first lifecycle command. No config => preflight never deployed.
        effective_path = safe_path(root, root / "runtime.effective.json", exists=False)
        if not state.get("effective_sha256"):
            if any(attempt.get("stages") for attempt in state["attempts"]):
                stand.fail("runtime: execution has no frozen effective configuration")
            state.update(status="deleted", deleted_at=now())
            atomic_json(root / "execution.json", state)
            return state
        effective = stand.read_json(effective_path)
        if hashlib.sha256(stand.canonical(effective)).hexdigest() != state["effective_sha256"]:
            stand.fail("runtime: changed effective config identity")
        groups = reserved_ports(snapshot["template"], state["reserved_ports"])
        fixed = {(service, target, "tcp", str(port)) for (service, target), port in groups.items()}
        # down operates on every service, including previous/orphan containers.
        validate_config(effective, root, list(effective.get("services", {})), state["project_name"], fixed=fixed,
                        publish_ip=publish_address(state.get("publish_ip", "127.0.0.1")))
        paths = deletion_bind_paths(root, effective, compose_path)
        if safe_path(root, root / ".runtime.env").read_text(encoding="utf-8"):
            stand.fail("runtime: controlled environment file was modified")
        log = safe_path(root, root / "logs" / ("delete-" + uuid.uuid4().hex + ".log"), exists=False)
        log.parent.mkdir(exist_ok=True)
        record = {"started_at": now(), "status": "running", "log": str(log.relative_to(root))}
        state.setdefault("deletions", []).append(record)
        atomic_json(root / "execution.json", state)
        try:
            executor = Executor(root, environment(root, snapshot), timeout, cancel=cancel or (lambda: False))
            executor.docker_check(log)
            compose = prefix(root, effective_path, state["project_name"])
            deletion_resources(executor, effective, compose, state["project_name"], log)
            executor.call([*compose, "down", "--volumes", "--remove-orphans"], log)
            for path in paths:
                executor.checkpoint()
                safe_path(root, path, exists=False)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            record.update(status="success", finished_at=now())
            state.update(status="deleted", deleted_at=now())
            atomic_json(root / "execution.json", state)
            return state
        except (Exception, KeyboardInterrupt) as exc:
            record.update(status="failed", finished_at=now(), error=str(exc))
            atomic_json(root / "execution.json", state)
            raise

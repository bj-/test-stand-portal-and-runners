"""Private data-adapter protocol and explicitly enabled managed runner overlays."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import ssl
import tempfile

from . import stand


def overlay_path(root, project, stand_id, binding):
    from . import runtime
    # Project codes historically permit punctuation; filenames must be portable.
    for name in (project, stand_id, binding):
        stand.relative_path(name, "managed overlay identifier")
        if "/" in name:
            stand.fail("managed overlay: single filename segment required")
    return runtime.safe_path(root, Path(root) / project / ".local" / (stand_id + "--" + binding + ".yaml"), exists=False)


def validate_output(output):
    stand.obj(output, ("ok",), ("environment", "ca_pem"), label="data adapter output")
    if output["ok"] is not True:
        stand.fail("data adapter: unsuccessful output")
    env = output.get("environment", {})
    if not isinstance(env, dict):
        stand.fail("data adapter: environment object required")
    result = {}
    seen = set()
    for name, value in env.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name.lower().startswith("__portal_") or name.casefold() in seen):
            stand.fail("data adapter: invalid or reserved environment name")
        seen.add(name.casefold())
        if not (value is None or type(value) in (str, int, float, bool)):
            stand.fail("data adapter: scalar environment values required")
        if isinstance(value, str) and "\x00" in value or isinstance(value, float) and not math.isfinite(value):
            stand.fail("data adapter: invalid environment value")
        result[name] = value
    if "ca_pem" in output:
        pem = output["ca_pem"]
        if (not isinstance(pem, str) or len(pem.encode("utf-8")) > 65536
                or pem.count("-----BEGIN CERTIFICATE-----") != 1
                or pem.count("-----END CERTIFICATE-----") != 1
                or not re.fullmatch(r"\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*", pem)):
            stand.fail("data adapter: one PEM certificate up to 64 KiB required")
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
        except (ssl.SSLError, ValueError):
            stand.fail("data adapter: invalid CA certificate")
        result["__portal_ca_pem"] = pem
    return result


def apply(executor, compose, snapshot, entries, *, project, stand_id, managed_root=None):
    """Never forward adapter stdout/stderr or retain input/output in build results."""
    from . import runtime
    pending = []
    for entry in entries:
        executor.checkpoint()
        module = snapshot["template"]["data_modules"][entry["module"]]
        if "run_binding" in module and managed_root is None:
            stand.fail("data adapter: run_binding requires configured managed_run_overlays")
        with tempfile.TemporaryDirectory(prefix=".portal-data-", dir=executor.root) as name:
            directory = Path(name)
            envelope = {"protocol": 1, "operation": "apply", "project": project, "stand": stand_id,
                        "target": entry["module"], "format": entry["format"], "data": entry["data"],
                        "query": None, "dataset": {"id": entry["dataset_id"], "version": entry["version"]}}
            runtime.atomic_json(directory / "input.json", envelope)
            # Even stderr and diagnostics are captured in this ephemeral private
            # directory, which is never scanned by Worker.logs.
            try:
                executor.call([*compose, "run", "--rm", "--no-deps", "-T", "--volume",
                               directory.as_posix() + ":/portal-data", "--env", "PORTAL_DATA_INPUT=/portal-data/input.json",
                               "--env", "PORTAL_DATA_OUTPUT=/portal-data/output.json", "--entrypoint", module["argv"][0],
                               module["service"], *module["argv"][1:]],
                              directory / "adapter.log", capture=True)
            except runtime.CommandFailed:
                raise runtime.CommandFailed("data adapter: module execution failed") from None
            output_path = runtime.safe_path(directory, directory / "output.json", exists=False)
            if not output_path.is_file() or output_path.stat().st_size > 2 * 1024 * 1024:
                stand.fail("data adapter: missing or oversized output")
            try:
                output = validate_output(stand.read_json(output_path))
            except (ValueError, OSError, UnicodeError, RecursionError):
                stand.fail("data adapter: invalid output")
            if "run_binding" in module:
                pending.append((module["run_binding"], output))
    return pending


def publish(root, pending, *, project, stand_id, target_host, workspace):
    """Validate every target and provenance before atomically replacing each file."""
    from . import runtime
    if not pending:
        return
    targets = []
    owner = {"schema_version": 1, "project": project, "stand": stand_id, "target_host": target_host,
             "workspace": str(workspace)}
    for binding, output in pending:
        path = overlay_path(root, project, stand_id, binding)
        sidecar = path.with_name(path.name + ".portal-owner.json")
        runtime.safe_path(root, sidecar, exists=False)
        if path.exists() or sidecar.exists():
            if not path.is_file() or not sidecar.is_file():
                stand.fail("managed overlay: existing manual settings cannot be overwritten")
            try:
                prior = stand.read_json(sidecar)
            except (ValueError, OSError, UnicodeError):
                stand.fail("managed overlay: invalid ownership provenance")
            if not isinstance(prior, dict) or any(prior.get(key) != value for key, value in owner.items()) or prior.get("binding") != binding:
                stand.fail("managed overlay: ownership mismatch")
            if prior.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
                stand.fail("managed overlay: modified settings cannot be overwritten")
        targets.append((path, sidecar, binding, output))
    # Keep backups in memory so a publication error cannot leave a partial set.
    backups = [(path, path.read_bytes() if path.exists() else None)
               for target in targets for path in target[:2]]
    try:
        for path, sidecar, binding, output in targets:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            runtime.atomic_json(path, output)
            runtime.atomic_json(sidecar, dict(owner, binding=binding, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    except BaseException:
        for path, previous in reversed(backups):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                # Restore exact contents and provenance without interpreting data.
                temporary = path.with_name(path.name + ".rollback")
                with temporary.open("xb") as stream:
                    stream.write(previous)
                temporary.replace(path)
        raise


def cleanup(root, snapshot, *, project, stand_id, target_host, workspace):
    """Remove only unchanged files generated for this exact worker workspace."""
    from . import runtime
    for module in snapshot["template"].get("data_modules", {}).values():
        binding = module.get("run_binding")
        if binding is None:
            continue
        path = overlay_path(root, project, stand_id, binding)
        sidecar = runtime.safe_path(root, path.with_name(path.name + ".portal-owner.json"), exists=False)
        if not path.is_file() or not sidecar.is_file():
            continue
        try:
            prior = stand.read_json(sidecar)
        except (ValueError, OSError, UnicodeError):
            continue
        expected = {"schema_version": 1, "project": project, "stand": stand_id, "target_host": target_host,
                    "workspace": str(workspace), "binding": binding}
        if (isinstance(prior, dict) and all(prior.get(key) == value for key, value in expected.items())
                and prior.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
            path.unlink()
            sidecar.unlink()

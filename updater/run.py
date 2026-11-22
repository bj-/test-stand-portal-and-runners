#!/usr/bin/env python3
"""Single-host, fixed-ref updater. Python 3 standard library; Linux production host."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit


SHA = re.compile(r"^[0-9a-f]{40}$")
POLICY_GENERATION = re.compile(r"[a-f0-9]{32}")
TERMINAL = {"unchanged", "available", "succeeded", "failed", "recovery_required", "paused"}
DANGEROUS = {"activating", "verifying", "rolling_back", "resuming"}
PAUSED_MESSAGE = "Installation canceled by the update pause"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        if path.stat().st_size > 65536:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class UpdateError(Exception):
    """Contains only a fixed public message, never subprocess output."""


class Config:
    def __init__(self, data):
        self.repository = data["repository"]
        self.ref = data.get("ref", "refs/heads/main")
        self.state_dir = Path(data["state_dir"])
        self.work_dir = Path(data["work_dir"])
        self.adapter = data["adapter"]
        if not isinstance(self.repository, str) or any(c in self.repository for c in "\r\n\0"):
            raise ValueError("Invalid repository")
        parts = urlsplit(self.repository)
        if (parts.scheme in {"http", "https"} and parts.username) or parts.password or parts.query or parts.fragment:
            raise ValueError("Repository must not contain credentials, query or fragment")
        if self.repository.startswith("-") or "::" in self.repository or (parts.scheme and parts.scheme not in {"https", "http", "ssh", "git", "file"} and not re.match(r"^[A-Za-z]:[/\\]", self.repository)):
            raise ValueError("Unsupported repository transport")
        if not isinstance(self.ref, str) or not self.ref.startswith(("refs/heads/", "refs/tags/")) or any(c in self.ref for c in "\r\n\0"):
            raise ValueError("A full branch or tag ref is required")
        if not isinstance(self.adapter, list) or not self.adapter or not all(isinstance(x, str) and x for x in self.adapter):
            raise ValueError("Adapter must be an argument array")
        if not Path(self.adapter[0]).is_absolute():
            raise ValueError("Adapter executable must have an absolute path")
        if not self.state_dir.is_absolute() or not self.work_dir.is_absolute():
            raise ValueError("State and work directories must have absolute paths")
        for name, default in (("interval_seconds", 3600), ("git_timeout_seconds", 60),
                              ("drain_timeout_seconds", 1800), ("poll_seconds", 3),
                              ("command_timeout_seconds", 300), ("prepare_timeout_seconds", 1800)):
            value = data.get(name, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts and intervals must be positive numbers")
            setattr(self, name, value)
        self.auto_apply = data.get("auto_apply", True)
        if not isinstance(self.auto_apply, bool):
            raise ValueError("auto_apply must be a boolean")


class Runner:
    def run(self, args, timeout, capture=False, output=None):
        # A process group is essential: timed-out compose/build children must not
        # mutate the host after we have started recovery. No output is logged.
        with tempfile.TemporaryFile() as result:
            target = output if output is not None else result if capture else subprocess.DEVNULL
            environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=target,
                                       stderr=subprocess.DEVNULL, env=environment,
                                       start_new_session=(os.name == "posix"))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                raise UpdateError("Command timed out") from None
            if process.returncode:
                raise UpdateError("Command failed")
            if capture:
                result.seek(0, os.SEEK_END)
                if result.tell() > 65536:
                    raise UpdateError("Command response exceeded the limit")
                result.seek(0)
                return result.read().decode("utf-8", errors="strict").strip()


@contextlib.contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        os.set_inheritable(stream.fileno(), False)
        if os.name != "posix":
            raise UpdateError("The updater daemon requires a Linux host")
        import fcntl
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("Another updater is running") from None
        yield


class Updater:
    def __init__(self, config, runner=None, monotonic=time.monotonic, sleep=time.sleep):
        self.config = config
        self.runner = runner or Runner()
        self.monotonic = monotonic
        self.sleep = sleep
        self.state = config.state_dir
        self.work = config.work_dir
        for directory in (self.state, self.work, self.state / "requests", self.state / "operations", self.work / "releases"):
            directory.mkdir(parents=True, exist_ok=True)
        self.work.chmod(0o755)
        (self.work / "releases").chmod(0o755)
        self.status = read_json(self.state / "status.json", {}) or {}
        self.internal = read_json(self.work / "worker.json", {}) or {}
        self.active = []
        self.effective_apply = False
        self.apply_generation = None
        self.next_check = self.monotonic()
        self.startup_pending = True
        self.script_path = Path(__file__).resolve()
        self.script_digest = hashlib.sha256(self.script_path.read_bytes()).digest()

    def adapter(self, command, *args, json_output=False, timeout=None):
        raw = self.runner.run(self.config.adapter + [command] + [str(x) for x in args],
                              timeout or self.config.command_timeout_seconds, capture=json_output)
        if json_output:
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except (ValueError, TypeError):
                raise UpdateError("Invalid adapter response") from None

    def save_status(self, phase, message):
        if phase == "recovery_required":
            self.internal["recovery_required"] = True
            if self.status.get("failure_phase"):
                self.internal["failure_phase"] = self.status["failure_phase"]
            atomic_json(self.work / "worker.json", self.internal)
        self.status.update(enabled=True, pause_supported=True, phase=phase, message=message, updated_at=now())
        self.status.setdefault("current_revision", "")
        self.status.setdefault("available_revision", "")
        self.status.setdefault("last_checked_at", None)
        self.status.setdefault("last_operation_id", None)
        atomic_json(self.state / "status.json", self.status)

    def policy(self):
        try:
            with (self.state / "policy.json").open("rb") as stream:
                raw = stream.read(32_769)
            data = json.loads(raw) if len(raw) <= 32_768 else None
        except FileNotFoundError:
            return False, None
        except (OSError, ValueError):
            return True, None
        if isinstance(data, dict):
            paused, generation = data.get("paused"), data.get("generation")
            if isinstance(paused, bool) and isinstance(generation, str) and POLICY_GENERATION.fullmatch(generation):
                return paused, generation
        return True, None

    def pending(self):
        requests = []
        for path in sorted((self.state / "requests").glob("*.json")):
            try:
                ident = str(uuid.UUID(path.stem))
            except ValueError:
                continue
            if path.stem != ident or (self.state / "operations" / path.name).exists():
                continue
            value = read_json(path)
            if not isinstance(value, dict) or value.get("id") != ident or value.get("action") not in {"check", "apply"}:
                continue
            # All text is untrusted; request attribution is bounded, control-free.
            who = value.get("requested_by", "admin")
            who = "".join(c for c in str(who) if c.isprintable())[:128]
            created = value.get("created_at")
            if not isinstance(created, str) or len(created) > 64 or any(ord(c) < 32 for c in created):
                created = now()
            requests.append(dict(id=ident, action=value["action"], requested_by=who, created_at=created,
                                 policy_generation=value.get("policy_generation")))
        return requests

    def attach(self, request):
        self.active.append({key: request[key] for key in ("id", "action", "requested_by", "created_at")}
                           | {"started_at": now()})

    def record_paused(self, request):
        operation = {key: request[key] for key in ("id", "action", "requested_by", "created_at")}
        timestamp = now()
        operation.update(phase="paused", message=PAUSED_MESSAGE, started_at=timestamp, finished_at=timestamp,
                         current_revision=self.status.get("current_revision", ""),
                         available_revision=self.status.get("available_revision", ""))
        atomic_json(self.state / "operations" / (operation["id"] + ".json"), operation)

    def phase(self, phase, message, finished=False):
        # Coalesce requests submitted during a long build/drain, preserving an
        # individually queryable operation record for every accepted request.
        can_join_apply = self.effective_apply and self.policy() == (False, self.apply_generation)
        active_ids = {operation["id"] for operation in self.active}
        for request in self.pending():
            same_policy = can_join_apply and request["policy_generation"] == self.apply_generation
            if request["id"] not in active_ids and (request["action"] == "check" or same_policy):
                self.attach(request)
        self.save_status(phase, message)
        for operation in self.active:
            operation.update(phase=phase, message=message,
                             current_revision=self.status.get("current_revision", ""),
                             available_revision=self.status.get("available_revision", ""))
            for field in ("failure_phase", "error_code"):
                if field in self.status:
                    operation[field] = self.status[field]
            if finished:
                operation["finished_at"] = now()
            atomic_json(self.state / "operations" / (operation["id"] + ".json"), operation)

    def recover_startup(self):
        previous_phase = self.status.get("phase")
        interrupted = []
        for path in (self.state / "operations").glob("*.json"):
            operation = read_json(path)
            if isinstance(operation, dict) and operation.get("phase") not in TERMINAL:
                interrupted.append((path, operation))
        unsafe = self.internal.get("recovery_required") is True or previous_phase == "recovery_required" or previous_phase in DANGEROUS or any(op.get("phase") in DANGEROUS for _, op in interrupted)
        if not unsafe and (previous_phase in {"draining", "backing_up"} or any(op.get("phase") in {"draining", "backing_up"} for _, op in interrupted)):
            try:
                self.adapter("health")
                self.adapter("resume")
            except Exception:
                unsafe = True
        phase = "recovery_required" if unsafe else "failed"
        message = "Manual recovery is required after an interrupted update" if unsafe else "Previous update was interrupted before activation"
        if unsafe or interrupted:
            failure_phase = self.internal.get("failure_phase") or previous_phase or "checking"
            self.status.update(failure_phase=failure_phase, error_code=self.status.get("error_code") or "update_failed")
        for path, operation in interrupted:
            operation.update(phase=phase, message=message, finished_at=now(),
                             failure_phase=operation.get("failure_phase") or operation.get("phase", "checking"), error_code="update_failed")
            atomic_json(path, operation)
        if unsafe or interrupted or previous_phase not in TERMINAL:
            self.save_status(phase if unsafe or interrupted else "idle", message if unsafe or interrupted else "Updater ready")
        elif self.status.get("pause_supported") is not True:
            self.save_status(previous_phase, self.status.get("message", "Updater ready"))

    def git(self, *args, capture=False, output=None):
        # No checkout, hooks, submodules, credentials in argv, or host git config.
        return self.runner.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.interactive=false",
                                "--git-dir=" + str(self.work / "repository.git"), *args],
                               self.config.git_timeout_seconds, capture=capture, output=output)

    def fetch(self):
        mirror = self.work / "repository.git"
        if not mirror.exists():
            self.runner.run(["git", "init", "--bare", str(mirror)], self.config.git_timeout_seconds)
        self.git("check-ref-format", self.config.ref)
        self.git("fetch", "--no-tags", "--force", self.config.repository, self.config.ref)
        revision = self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}", capture=True)
        if not SHA.fullmatch(revision):
            raise UpdateError("Invalid fetched revision")
        return revision

    def export(self, revision):
        destination = self.work / "releases" / revision
        if destination.is_dir():
            return destination
        # Never follow repository links or extract device files. Stage first so a
        # crash cannot make a partially exported release look complete.
        staging = Path(tempfile.mkdtemp(prefix=".export-", dir=self.work / "releases"))
        with tempfile.TemporaryFile() as archive_file:
            self.git("archive", "--format=tar", revision, output=archive_file)
            archive_file.seek(0)
            with tarfile.open(fileobj=archive_file, mode="r:") as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if not parts or member.name.startswith(("/", "\\")) or ".." in parts or "\\" in member.name or ":" in member.name or not (member.isdir() or member.isfile()):
                        raise UpdateError("Release contains an unsafe archive entry")
                    target = staging.joinpath(*parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        target.chmod(0o755)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        for parent in target.parents:
                            if parent == staging:
                                break
                            parent.chmod(0o755)
                        with archive.extractfile(member) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
                        target.chmod(0o755 if member.mode & 0o111 else 0o644)
        staging.chmod(0o755)
        staging.rename(destination)
        return destination

    def healthy_resume(self):
        self.adapter("health")
        self.adapter("resume")

    def execute(self, request, automatic=False):
        paused, generation = self.policy()
        if automatic and paused:
            return
        if request["action"] == "apply" and (paused or request.get("policy_generation") != generation):
            self.record_paused(request)
            return
        self.active = []
        self.effective_apply = request["action"] == "apply" or (automatic and self.config.auto_apply)
        self.apply_generation = generation
        self.attach(request)
        self.status["last_operation_id"] = request["id"]
        blocked = self.internal.get("recovery_required") is True or self.status.get("phase") == "recovery_required"
        if not blocked:
            self.status.pop("failure_phase", None)
            self.status.pop("error_code", None)
        disruptive = activated = False
        admission_reopening = False
        candidate = None
        try:
            if blocked and self.effective_apply:
                self.phase("recovery_required", "Manual recovery is required before applying updates", finished=True)
                return
            self.phase("checking", "Checking configured source")
            self.status["last_checked_at"] = now()
            installed = self.adapter("current", json_output=True).get("revision", "")
            if not isinstance(installed, str) or installed and not SHA.fullmatch(installed):
                raise UpdateError("Invalid installed revision")
            self.status["current_revision"] = installed
            candidate = self.fetch()
            self.status.update(available_revision=candidate, last_checked_at=now())
            if blocked:
                self.phase("recovery_required", "Source checked; manual recovery is still required", finished=True)
                return
            if candidate == installed:
                self.phase("unchanged", "Installed revision is current", finished=True)
                return
            if not self.effective_apply:
                self.phase("available", "An update is available", finished=True)
                return
            if self.policy() != (False, self.apply_generation):
                self.phase("paused", PAUSED_MESSAGE, finished=True)
                return
            if automatic and self.internal.get("failed_revision") == candidate:
                self.phase("failed", "This revision previously failed; an explicit apply is required to retry", finished=True)
                return
            self.phase("preparing", "Preparing candidate without changing the running portal")
            source = self.export(candidate)
            prepared = self.work / "releases" / (candidate + ".prepared.json")
            if read_json(prepared, {}).get("revision") != candidate:
                self.adapter("prepare", source, candidate, timeout=self.config.prepare_timeout_seconds)
                atomic_json(prepared, {"revision": candidate})
            if self.policy() != (False, self.apply_generation):
                self.phase("paused", PAUSED_MESSAGE, finished=True)
                return
            self.phase("draining", "Waiting for active runs to finish")
            disruptive = True
            result = self.adapter("drain", json_output=True)
            deadline = self.monotonic() + self.config.drain_timeout_seconds
            while True:
                running = result.get("running")
                if result.get("maintenance") is not True or not isinstance(running, int) or isinstance(running, bool) or running < 0:
                    raise UpdateError("Invalid drain response")
                if running == 0:
                    break
                if self.monotonic() >= deadline:
                    raise UpdateError("Timed out waiting for active runs")
                self.sleep(min(self.config.poll_seconds, max(0, deadline - self.monotonic())))
                self.phase("draining", "Waiting for active runs to finish")
                result = self.adapter("idle", json_output=True, timeout=min(self.config.command_timeout_seconds, max(0.1, deadline - self.monotonic())))
            self.phase("backing_up", "Backing up database before activation")
            backup = self.adapter("backup", request["id"], json_output=True).get("backup")
            if not isinstance(backup, str) or not backup:
                raise UpdateError("Adapter did not confirm a database backup")
            self.phase("activating", "Activating prepared candidate")
            activated = True  # persist before invoking an operation that may partially fail
            self.adapter("activate", source, candidate)
            self.phase("verifying", "Checking activated portal")
            self.adapter("health")
            self.phase("resuming", "Resuming workload admission")
            admission_reopening = True
            self.adapter("resume")
            self.status["current_revision"] = candidate
            self.internal.pop("failed_revision", None)
            atomic_json(self.work / "worker.json", self.internal)
            self.phase("succeeded", "Update completed", finished=True)
        except Exception as error:
            # Never publish exception text: argv, Git output and adapter exceptions
            # can carry credentials or customer-specific details.
            if not blocked or "failure_phase" not in self.status:
                self.status["failure_phase"] = self.status.get("phase", "checking")
                self.status["error_code"] = "command_timeout" if isinstance(error, UpdateError) and str(error) == "Command timed out" else "update_failed"
            if candidate and self.effective_apply:
                self.internal["failed_revision"] = candidate
                atomic_json(self.work / "worker.json", self.internal)
                # An explicit retry must reconstruct images that may have been
                # pruned or partially replaced. Automatic retries remain blocked.
                (self.work / "releases" / (candidate + ".prepared.json")).unlink(missing_ok=True)
            safe = not blocked
            if admission_reopening:
                # Admission may already be open and new jobs may have started.
                # Rolling back containers now could terminate those jobs.
                safe = False
            elif activated:
                self.phase("rolling_back", "Checking whether the previous application can be restored safely")
                try:
                    self.adapter("rollback")
                    self.healthy_resume()
                except Exception:
                    safe = False
            elif disruptive:
                try:
                    self.healthy_resume()
                except Exception:
                    safe = False
            self.phase("failed" if safe else "recovery_required",
                       "Update failed; the installed portal was preserved or restored" if safe else "Update stopped; manual recovery and workload admission checks are required",
                       finished=True)
        finally:
            self.active = []

    def tick(self):
        pending = self.pending()
        if pending:
            # Prefer apply, so simultaneous checks share its preparation/result.
            request = next((x for x in pending if x["action"] == "apply"), pending[0])
            self.execute(request)
            return
        if self.policy()[0]:
            self.next_check = self.monotonic()
            return
        if self.monotonic() >= self.next_check:
            if self.startup_pending:
                try:
                    self.adapter("health", timeout=min(10, self.config.command_timeout_seconds))
                except Exception:
                    return  # independently running portal may still be booting
                self.startup_pending = False
            self.next_check = self.monotonic() + self.config.interval_seconds
            self.execute(dict(id=str(uuid.uuid4()), action="check", requested_by="scheduler", created_at=now()), automatic=True)

    def run(self, once=False):
        self.recover_startup()
        if once:
            self.tick()
            return
        while True:
            self.tick()
            self.restart_if_updated()
            self.sleep(self.config.poll_seconds)

    def restart_if_updated(self):
        if self.status.get("phase") not in {"succeeded", "unchanged"}:
            return
        try:
            changed = hashlib.sha256(self.script_path.read_bytes()).digest() != self.script_digest
        except OSError:
            return
        if changed:
            # Adapter atomically installs the script. All operation records have
            # reached a terminal phase; the lock descriptor closes during exec.
            os.execv(sys.executable, [sys.executable, *sys.argv])

    def acknowledge_recovery(self):
        """Operator has repaired app/schema; verify before reopening admission."""
        self.recover_startup()
        self.adapter("health")
        revision = self.adapter("current", json_output=True).get("revision")
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise UpdateError("A valid installed revision is required")
        self.adapter("resume")
        self.status["current_revision"] = revision
        self.internal.pop("recovery_required", None)
        self.internal.pop("failure_phase", None)
        self.status.pop("failure_phase", None)
        self.status.pop("error_code", None)
        atomic_json(self.work / "worker.json", self.internal)
        self.save_status("idle", "Manual recovery verified; updater ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--acknowledge-recovery", action="store_true",
                        help="After manual app/schema repair: verify health and reopen workload admission")
    args = parser.parse_args()
    try:
        config = Config(read_json(Path(args.config)))
        with process_lock(config.state_dir / "worker.lock"):
            updater = Updater(config)
            if args.acknowledge_recovery:
                updater.acknowledge_recovery()
            else:
                updater.run(once=args.once)
    except (Exception, KeyboardInterrupt):
        print("Updater stopped; inspect updater state and host configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

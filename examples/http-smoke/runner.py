"""Проверки HTTP API; exit 0/1, JUnit XML и JSON во внешнем OUTPUT_DIR."""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def check(base_url, path, expected, wait_seconds=0):
    started = time.monotonic()
    deadline = started + wait_seconds
    while True:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=2) as response:
                body = json.load(response)
                if response.status != 200 or body != expected:
                    raise ValueError(f"Expected HTTP 200 and {expected!r}; received {response.status} and {body!r}")
            return {"name": path, "passed": True, "duration": time.monotonic() - started, "message": ""}
        except (OSError, ValueError, urllib.error.URLError) as error:
            if time.monotonic() >= deadline:
                return {"name": path, "passed": False, "duration": time.monotonic() - started, "message": str(error)}
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def run(base_url, output_dir, wait_seconds=30):
    started = time.monotonic()
    checks = [
        check(base_url, "/health", {"status": "ok"}, wait_seconds),
        check(base_url, "/version", {"version": "1.0.0"}),
    ]
    failures = sum(not item["passed"] for item in checks)
    result = {
        "suite": "http-smoke",
        "finished": datetime.now(timezone.utc).isoformat(),
        "tests": len(checks),
        "failures": failures,
        "duration": time.monotonic() - started,
        "checks": checks,
    }
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    suite = ET.Element("testsuite", name=result["suite"], tests=str(len(checks)), failures=str(failures), errors="0", time=f"{result['duration']:.6f}")
    for item in checks:
        case = ET.SubElement(suite, "testcase", classname="http_smoke", name=item["name"], time=f"{item['duration']:.6f}")
        if not item["passed"]:
            ET.SubElement(case, "failure", message=item["message"]).text = item["message"]
    ET.indent(suite)
    ET.ElementTree(suite).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
    print(f"http-smoke: {len(checks) - failures}/{len(checks)} passed; reports: {directory}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://app:8080"))
    parser.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR"))
    parser.add_argument("--wait-seconds", type=float, default=30, help="Ожидание успешного /health; HTTP timeout: 2 секунды")
    args = parser.parse_args()
    if not args.output_dir:
        parser.error("Set OUTPUT_DIR or pass --output-dir (external results directory)")
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be nonnegative")
    return run(args.base_url, args.output_dir, args.wait_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

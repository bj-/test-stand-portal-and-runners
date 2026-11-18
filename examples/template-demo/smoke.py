"""HTTP smoke с JSON, JUnit и исходными результатами Allure."""
import json
import os
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


def run(base_url, output):
    output = Path(output)
    allure = output / "allure-results"
    allure.mkdir(parents=True, exist_ok=False)
    cases = []
    for path, expected in [("/health", {"status": "ok"}),
                           ("/items", {"items": [{"id": 1, "name": "demo"}]})]:
        start = int(time.time() * 1000)
        error = None
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10) as response:
                actual = json.load(response)
                if response.status != 200 or actual != expected:
                    raise ValueError(f"Unexpected response: {response.status}, {actual!r}")
        except (OSError, ValueError) as exc:
            error = str(exc)
        stop = int(time.time() * 1000)
        uid = str(uuid.uuid4())
        record = {"uuid": uid, "name": path if not error else "❔ не разобрано · " + path,
                  "fullName": "template_demo" + path, "historyId": "template_demo" + path,
                  "status": "failed" if error else "passed", "stage": "finished",
                  "start": start, "stop": stop,
                  "labels": [{"name": "suite", "value": "Учебный стенд"}]}
        if error:
            record["statusDetails"] = {"message": error}
        (allure / (uid + "-result.json")).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        cases.append({"name": path, "passed": error is None, "message": error,
                      "duration": (stop - start) / 1000})
    failures = sum(not case["passed"] for case in cases)
    (output / "results.json").write_text(json.dumps({"tests": len(cases), "failures": failures, "checks": cases}, indent=2), encoding="utf-8")
    suite = ET.Element("testsuite", name="template-demo", tests=str(len(cases)), failures=str(failures))
    for case in cases:
        node = ET.SubElement(suite, "testcase", name=case["name"], classname="template_demo", time=str(case["duration"]))
        if not case["passed"]:
            ET.SubElement(node, "failure", message=case["message"])
    ET.ElementTree(suite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
    (output / "report.md").write_text(f"# Smoke учебного стенда\n\nПроверок: {len(cases)}, ошибок: {failures}.\n" +
        ("Падения требуют разбора; результат не принят.\n" if failures else "Проверки прошли.\n"), encoding="utf-8")
    # Reporter must run even on failed checks, then return failure after HTML is ready.
    return failures


if __name__ == "__main__":
    failures = run(os.environ.get("BASE_URL", "http://api:8080"), os.environ.get("OUTPUT_DIR", "/results"))
    print(f"Smoke: {failures} failed; final exit status is set by the report container")

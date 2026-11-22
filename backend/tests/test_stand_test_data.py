"""Project bundles remain pinned, private, project-scoped and atomic."""
import copy

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from portal import auth
from portal.models import Dataset, DatasetVersion, Job, Project
from portal.stand_test_data import MAX_DATA, seed_payload
from test_managed_stands import body, client, create, installation, register, session, template


def initial(module="api", key="basic", **extra):
    return {"module": module, "key": key, "title": "Basic", "format": "example-v1",
            "data": {"private": "never-in-status"}, **extra}


def add_dataset(client, code="ONE", **extra):
    item = initial(**extra)
    del item["module"]
    response = client.post(f"/api/projects/{code}/datasets", json=item)
    assert response.status_code == 201, response.text
    return response.json()


def put_bundle(client, entries, code="ONE", title="Default"):
    return client.put(f"/api/projects/{code}/stand-data-bundle", json={"title": title, "entries": entries})


def reference(dataset, module="api", version=1):
    return {"module": module, "dataset_id": dataset["id"], "version": version}


def adapters(**extra):
    spec = template()
    spec["data_modules"] = {"api": {"title": "API data", "service": "api", "format": "example-v1",
                                     "argv": ["seed", "--stdin"], **extra}}
    return spec


def test_project_creation_builds_atomic_dataset_versions_and_default_bundle(client, session):
    response = client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {},
                          "initial_test_data": [initial(), initial("other", "second")]})
    assert response.status_code == 201, response.text
    assert "never-in-status" not in response.text
    bundle = client.get("/api/projects/NEW/stand-data-bundle").json()
    assert bundle["title"] == "Базовый комплект"
    assert [item["module"] for item in bundle["entries"]] == ["api", "other"]
    assert "data" not in bundle["entries"][0]
    for entry in bundle["entries"]:
        version = session.get(DatasetVersion, (entry["dataset_id"], 1))
        assert version.data == {"private": "never-in-status"}
        assert version.created_by == "admin"
    assert session.get(Project, "NEW").stand_data_bundle == bundle


@pytest.mark.parametrize("items", [[initial(), initial()], [initial(), initial("other")]])
def test_invalid_initial_bundle_rolls_back_entire_project(client, session, items):
    response = client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {},
                          "initial_test_data": items})
    assert response.status_code == 422, response.text
    assert session.get(Project, "NEW") is None
    assert session.scalar(select(func.count()).select_from(Dataset)) == 0
    assert session.scalar(select(func.count()).select_from(DatasetVersion)) == 0


def test_null_and_yaml_fallback_read_is_pure_put_lazily_indexes(client, session):
    default = {"title": "Базовый комплект", "entries": []}
    assert client.get("/api/projects/ONE/stand-data-bundle").json() == default
    assert client.get("/api/projects/YAML/stand-data-bundle").json() == default
    assert session.get(Project, "YAML") is None
    response = put_bundle(client, [], code="YAML", title="Explicit")
    assert response.status_code == 200, response.text
    assert session.get(Project, "YAML").stand_data_bundle == {"title": "Explicit", "entries": []}


@pytest.mark.parametrize("role,read,write", [("admin", 200, 200), ("tester", 200, 200),
                                            ("developer", 200, 403), ("viewer", 403, 403)])
def test_bundle_role_access(client, session, role, read, write):
    client.cookies.set(auth.COOKIE, role)
    assert client.get("/api/projects/ONE/stand-data-bundle").status_code == read
    assert put_bundle(client, []).status_code == write


def test_bundle_order_pinning_and_foreign_versions_validation(client, session):
    one, two = add_dataset(client), add_dataset(client, key="second")
    entries = [reference(two, "other"), reference(one)]
    response = put_bundle(client, entries)
    assert response.status_code == 200, response.text
    assert [item["dataset_id"] for item in response.json()["entries"]] == [two["id"], one["id"]]
    assert client.post(f"/api/projects/ONE/datasets/{one['id']}/versions", json={"data": {"new": True}}).status_code == 201
    assert client.get("/api/projects/ONE/stand-data-bundle").json()["entries"][1]["version"] == 1
    before = copy.deepcopy(session.get(Project, "ONE").stand_data_bundle)
    foreign = add_dataset(client, "TWO")
    for invalid in ([reference(foreign)], [reference(one, version=99)], [reference(one), reference(two)]):
        assert put_bundle(client, invalid).status_code == 422
        assert session.get(Project, "ONE").stand_data_bundle == before


def test_default_install_plan_pins_version_claim_only_has_raw_payload(client, session):
    dataset = add_dataset(client)
    expected = put_bundle(client, [reference(dataset)]).json()
    register(client, spec=adapters())
    catalog = client.get("/api/projects/ONE/stand-templates").json()[0]
    assert catalog["data_modules"] == {"api": {"title": "API data", "service": "api", "format": "example-v1"}}
    preflight = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["data_bundle"] == expected
    response = create(client)
    assert response.status_code == 201, response.text
    assert response.json()["data_bundle"] == expected
    job = session.get(Job, response.json()["job_id"])
    assert job.spec["test_data"] == expected
    assert "never-in-status" not in response.text
    assert "never-in-status" not in str(job.spec)
    assert client.post(f"/api/projects/ONE/datasets/{dataset['id']}/versions", json={"data": {"new": True}}).status_code == 201
    assert seed_payload(session, job) == [{**reference(dataset), "format": "example-v1",
                                         "data": {"private": "never-in-status"}}]
    assert "never-in-status" not in client.get(f"/api/projects/ONE/stand-instances/{job.target}").text


@pytest.mark.parametrize("with_adapter,opt_out,format", [(True, True, "wrong"), (False, False, "example-v1")])
def test_opt_out_and_legacy_template_have_empty_plan(client, session, with_adapter, opt_out, format):
    dataset = add_dataset(client, format=format)
    assert put_bundle(client, [reference(dataset)]).status_code == 200
    register(client, spec=adapters() if with_adapter else template())
    response = create(client, body(install_test_data=not opt_out))
    assert response.status_code == 201, response.text
    job = session.get(Job, response.json()["job_id"])
    assert "test_data" not in job.spec
    assert response.json()["data_bundle"] is None
    assert seed_payload(session, job) == []


def test_active_format_mismatch_is_422_and_inactive_module_is_skipped(client, session):
    dataset = add_dataset(client, format="wrong")
    put_bundle(client, [reference(dataset)])
    spec = adapters()
    spec["profiles"]["full"] = {"services": ["api", "optional"], "resources": {"cpus": 1, "memory_mb": 512}}
    spec["data_modules"]["unused"] = {"service": "optional", "format": "example-v1", "argv": ["seed"]}
    register(client, spec=spec)
    response = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert response.status_code == 422 and "формат" in response.json()["detail"]
    put_bundle(client, [reference(dataset, "unused")])
    response = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert response.status_code == 200, response.text
    assert response.json()["data_bundle"]["entries"] == []


def test_claim_rejects_foreign_project_and_aggregate_size(client, session):
    dataset = add_dataset(client)
    plan = put_bundle(client, [reference(dataset)]).json()
    job = Job(project="TWO", spec={"test_data": plan})
    with pytest.raises(HTTPException) as error:
        seed_payload(session, job)
    assert error.value.status_code == 422
    one = add_dataset(client, key="big1", data={"value": "x" * (MAX_DATA // 2)})
    two = add_dataset(client, key="big2", data={"value": "x" * (MAX_DATA // 2)})
    assert put_bundle(client, [reference(one), reference(two, "other")]).status_code == 422
    plan["entries"] = [{**reference(one), "format": "example-v1"}, {**reference(two, "other"), "format": "example-v1"}]
    job.project, job.spec = "ONE", {"test_data": plan}
    with pytest.raises(HTTPException) as error:
        seed_payload(session, job)
    assert error.value.status_code == 422

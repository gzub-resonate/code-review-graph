"""Service-name aliases for the Feign join and the infrastructure join of code repos.

Everything here is synthetic: repo, service, queue and bucket names are made up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph import service_aliases as sa
from code_review_graph.aws_config_names import resolve_config_names
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools.query import query_graph
from code_review_graph.tools.registry_tools import cross_repo_requests_func

# -- Fixtures ------------------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv(sa.ALIASES_FILE_ENV, raising=False)
    return Registry()


def _client(root: Path, line: int, method: str, route: str, service: str) -> NodeInfo:
    return NodeInfo(
        kind="Endpoint", name=f"call{line}@GetMapping[0:0] {method} {route}",
        file_path=str(root / "Client.java"), line_start=line, line_end=line,
        language="java", parent_name="Client",
        extra={
            "role": "client", "client": "feign", "http_method": method, "route": route,
            "config_keys": [], "route_dynamic": False, "url_literal": False,
            "service": service,
        },
    )


def _server(root: Path, line: int, method: str, route: str) -> NodeInfo:
    return NodeInfo(
        kind="Endpoint", name=f"h{line}@GetMapping[0:0] {method} {route}",
        file_path=str(root / "Controller.java"), line_start=line, line_end=line,
        language="java", parent_name="Controller",
        extra={"http_method": method, "route": route, "handler": f"h{line}"},
    )


def make_repo(
    tmp_path: Path, registry: Registry, name: str, files: dict[str, str] | None = None,
    nodes: tuple[NodeInfo, ...] = (), *, build=None,
) -> Path:
    """Write *files*, parse them into a graph beside synthetic endpoint *nodes*, register."""
    root = (tmp_path / name).resolve()
    (root / ".code-review-graph").mkdir(parents=True)
    parser = CodeParser(root)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        for rel, text in (files or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            file_nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), file_nodes, edges)
        for file_name in ("Client.java", "Controller.java"):
            store.store_file_nodes_edges(
                str(root / file_name),
                [n for n in nodes if n.file_path.endswith(file_name)], [],
            )
        resolve_config_names(store._conn)
    registry.register(str(root), alias=name)
    return root


def _calls(client_root: Path, *specs: tuple[int, str, str, str]) -> tuple[NodeInfo, ...]:
    return tuple(_client(client_root, *spec) for spec in specs)


def _row(reply: dict, line: int) -> dict:
    return next(r for r in reply["results"] if r["client_line"] == line)


APP_NAME_YML = "spring:\n  application:\n    name: stock-ledger\nserver:\n  port: 8080\n"


@pytest.fixture
def world(tmp_path, registry):
    """``web-app`` calls services that are registered under other names."""
    server_root = (tmp_path / "inventory-svc").resolve()
    make_repo(
        tmp_path, registry, "inventory-svc",
        {"src/main/resources/application.yml": APP_NAME_YML},
        (_server(server_root, 10, "GET", "/stock/{sku}"),),
    )
    billing_root = (tmp_path / "billing-core").resolve()
    make_repo(
        tmp_path, registry, "billing-core", None,
        (_server(billing_root, 10, "GET", "/accounts/{id}"),
         _server(billing_root, 20, "GET", "/deliveries/{id}")),
    )
    web = (tmp_path / "web-app").resolve()
    make_repo(tmp_path, registry, "web-app", None, _calls(
        web,
        (1, "GET", "/stock/{sku}", "stock-ledger"),
        (2, "GET", "/stock/{sku}", "Stock_Ledger-qa"),
        (3, "GET", "/accounts/{id}", "billing-api-accounts"),
        (4, "GET", "/deliveries/{id}", "billing-api-deliveries"),
        (5, "GET", "/accounts/{id}", "shared-catalog"),
        (6, "GET", "/stock/{sku}", "inventory-svc"),
    ))
    return tmp_path


# -- Pure rules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("Orders_API-prod", "orders-api"),
    ("orders-api", "orders-api"),
    ("orders__api--QA", "orders-api"),
    ("orders", "orders"),
    ("dev", "dev"),
    ("api-staging", "api"),
    ("orders-api-gateway", "orders-api-gateway"),
])
def test_normalize_identity(raw, expected):
    assert sa.normalize_identity(raw) == expected


def test_alias_file_formats_and_bad_entries(tmp_path, caplog):
    as_json = tmp_path / "a.json"
    as_json.write_text(json.dumps({
        "shared-catalog": "catalog-svc", "prefix:billing-api-": "billing-core",
        "prefix:billing-api-x-": "other", "bad name!": "x", "empty": "", "n": 3,
    }))
    rules = sa.load_alias_rules(str(as_json))
    assert rules.target_for("shared-catalog") == "catalog-svc"
    assert rules.target_for("Shared_Catalog") == "catalog-svc"
    assert rules.target_for("billing-api-accounts") == "billing-core"
    assert rules.target_for("billing-api-x-y") == "other"  # longest prefix first
    assert rules.target_for("unrelated") is None
    as_yaml = tmp_path / "a.yaml"
    as_yaml.write_text("aliases:\n  one: two\n  prefix:p-: three\n")
    yaml_rules = sa.load_alias_rules(str(as_yaml))
    assert yaml_rules.target_for("one") == "two" and yaml_rules.target_for("p-z") == "three"
    broken = tmp_path / "b.json"
    broken.write_text("{not json")
    assert not sa.load_alias_rules(str(broken))
    assert not sa.load_alias_rules(str(tmp_path / "missing.json"))
    assert not sa.load_alias_rules("")


# -- The Feign join ------------------------------------------------------------------------------


def test_before_aliases_nothing_resolves_but_exact_names(world):
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    # No alias file, no inference source named in the calls: the old behaviour holds.
    assert _row(reply, 3)["status"] == "no_target_repo"
    assert _row(reply, 5)["status"] == "no_target_repo"
    assert _row(reply, 6)["status"] == "matched"
    assert _row(reply, 6)["matched_via"] == "repo_name"


def test_spring_application_name_is_inferred(world):
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    exact = _row(reply, 1)
    assert exact["status"] == "matched"
    assert exact["matched_via"] == "spring_application_name"
    assert exact["target_resolution"] == "inferred"
    assert exact["candidates"][0]["repo"] == "inventory-svc"
    # Case, separators and an environment suffix fold; nothing else does.
    assert _row(reply, 2)["status"] == "matched"
    assert _row(reply, 2)["matched_via"] == "spring_application_name"


def test_explicit_alias_file_with_prefix_rule(world, monkeypatch, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({
        "prefix:billing-api-": "billing-core", "shared-catalog": "billing-core",
    }))
    monkeypatch.setenv(sa.ALIASES_FILE_ENV, str(path))
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    for line in (3, 4, 5):
        row = _row(reply, line)
        assert row["status"] == "matched", row
        assert row["matched_via"] == "explicit_alias"
        assert row["candidates"][0]["repo"] == "billing-core"
    assert reply["counts"]["matched"] == 6


def test_explicit_alias_to_unregistered_repo_is_ignored(world, monkeypatch, tmp_path):
    path = tmp_path / "aliases.yml"
    path.write_text("shared-catalog: nowhere\n")
    monkeypatch.setenv(sa.ALIASES_FILE_ENV, str(path))
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    assert _row(reply, 5)["status"] == "no_target_repo"


def test_ambiguous_inferred_name_lists_candidates_and_never_guesses(
    world, tmp_path, registry,
):
    twin = (tmp_path / "inventory-twin").resolve()
    make_repo(
        tmp_path, registry, "inventory-twin",
        {"src/main/resources/application.yml": APP_NAME_YML},
        (_server(twin, 10, "GET", "/stock/{sku}"),),
    )
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    row = _row(reply, 1)
    assert row["status"] == "ambiguous"
    assert row["reason"] == "service_name_ambiguous"
    assert sorted(row["candidate_repos"]) == ["inventory-svc", "inventory-twin"]
    assert "candidates" not in row and "matched_via" not in row
    # Still resolvable by an explicit rule, which outranks inference.


def test_explicit_rule_outranks_inference(world, monkeypatch, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"stock-ledger": "inventory-svc"}))
    monkeypatch.setenv(sa.ALIASES_FILE_ENV, str(path))
    row = _row(cross_repo_requests_func(repos=["web-app"], origin="feign"), 1)
    assert row["matched_via"] == "explicit_alias"


def test_ecr_and_ecs_names_of_a_service_repo_identify_it(tmp_path, registry):
    root = (tmp_path / "parcel-svc").resolve()
    make_repo(
        tmp_path, registry, "parcel-svc",
        {"deploy/main.tf": (
            'resource "aws_ecr_repository" "app" {\n  name = "parcel-tracker"\n}\n'
            'resource "aws_ecs_service" "svc" {\n  name = "parcel-runner-prod"\n}\n'
        )},
        (_server(root, 10, "GET", "/parcels"),),
    )
    web = (tmp_path / "web-app").resolve()
    make_repo(tmp_path, registry, "web-app", None, _calls(
        web, (1, "GET", "/parcels", "parcel-tracker"), (2, "GET", "/parcels", "parcel-runner"),
    ))
    reply = cross_repo_requests_func(repos=["web-app"], origin="feign")
    assert _row(reply, 1)["matched_via"] == "ecr_repo"
    assert _row(reply, 2)["matched_via"] == "ecs_service"


def test_an_infrastructure_repo_does_not_claim_the_names_it_declares(tmp_path, registry):
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    make_repo(tmp_path, registry, "infra-live", {"main.tf": "".join(
        f'resource "aws_ecr_repository" "{n}" {{\n  name = "{n}-svc"\n}}\n' for n in names
    )})
    web = (tmp_path / "web-app").resolve()
    make_repo(tmp_path, registry, "web-app", None, _calls(web, (1, "GET", "/x", "alpha-svc")))
    row = _row(cross_repo_requests_func(repos=["web-app"], origin="feign"), 1)
    assert row["status"] == "no_target_repo"


# -- The infrastructure join ---------------------------------------------------------------------

CODE_APP = """\
package app;

import org.springframework.beans.factory.annotation.Value;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;

public class Sender {
    private final SqsClient sqs;

    @Value("${app.queue.name}")
    private String queueName;

    public Sender(SqsClient sqs) {
        this.sqs = sqs;
    }

    void viaConfig(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl(queueName).messageBody(body).build());
    }

    void viaLiteral(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl("https://sqs.us-east-1.amazonaws.com/123456789012/stock-events")
            .messageBody(body).build());
    }
}
"""
INFRA_TF = """\
resource "aws_ecr_repository" "app" {
  name = "stock-ledger"
}

resource "aws_ecs_service" "svc" {
  name = "inventory-svc-prod"
}

resource "aws_ecs_task_definition" "task" {
  family = "stock-ledger"
}

resource "aws_sqs_queue" "events" {
  name = "stock-events"
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs-prod"
}

resource "aws_sqs_queue" "unrelated" {
  name = "something-else"
}
"""


@pytest.fixture
def infra_world(tmp_path, registry):
    make_repo(
        tmp_path, registry, "inventory-svc",
        {
            "src/main/java/app/Sender.java": CODE_APP,
            "src/main/resources/application.yml": (
                APP_NAME_YML + "app:\n  queue:\n    name: jobs-dev\n"
            ),
            "src/main/resources/application-prod.yml": "app:\n  queue:\n    name: jobs-prod\n",
        },
    )
    make_repo(
        tmp_path, registry, "infra-live",
        {
            "modules/ecs/main.tf": INFRA_TF,
            "stacks/inventory-svc/terragrunt.hcl": (
                'terraform {\n  source = "git::ssh://git@example.com/acme/mods.git//ecs?ref=v1"\n}\n'
            ),
            "stacks/other-thing/terragrunt.hcl": 'inputs = {\n  a = "b"\n}\n',
        },
    )
    return tmp_path


def _infra(reply: dict, repo: str = "inventory-svc") -> dict:
    return reply["infrastructure"]["repos"][repo]


def test_terraform_origin_relates_a_code_repo_to_its_infrastructure(infra_world):
    reply = cross_repo_requests_func(origin="terraform", repos=["inventory-svc"])
    assert reply["status"] == "ok" and reply["total"] == 0  # no module sources in the code repo
    info = _infra(reply)
    assert {i["name"] for i in info["ecr_repositories"]} == {"stock-ledger"}
    ecr = info["ecr_repositories"][0]
    assert ecr["repo"] == "infra-live" and ecr["file"] == "modules/ecs/main.tf"
    assert ecr["line"] == 1 and ecr["matched_via"] == "spring_application_name"
    assert info["ecs_services"][0]["name"] == "inventory-svc-prod"
    assert info["ecs_services"][0]["matched_via"] == "repo_name"
    assert info["task_definitions"][0]["matched_via"] == "spring_application_name"
    stack = info["stacks"][0]
    assert (stack["name"], stack["matched_via"]) == ("inventory-svc", "repo_name")
    assert stack["stack_kind"] == "terragrunt_stack" and stack["repo"] == "infra-live"
    resources = {r["name"]: r for r in info["resources"]}
    assert set(resources) == {"stock-events", "jobs-prod"}
    assert resources["stock-events"]["matched_via"] == "literal"
    assert resources["jobs-prod"]["matched_via"] == "resolved_name"
    assert resources["jobs-prod"]["profiles"] == ["prod"]
    assert resources["jobs-prod"]["file"] == "modules/ecs/main.tf"
    assert info["counts"]["resources"] == 2
    assert "infrastructure matched for 1 repo(s)" in reply["summary"]


def test_infrastructure_uses_alias_file_rules(infra_world, monkeypatch, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"prefix:stock-": "inventory-svc"}))
    monkeypatch.setenv(sa.ALIASES_FILE_ENV, str(path))
    info = _infra(cross_repo_requests_func(origin="terraform", repos=["inventory-svc"]))
    via = {i["name"]: i["matched_via"] for i in info["ecr_repositories"]}
    assert via == {"stock-ledger": "explicit_alias"}
    assert any(i["name"] == "prefix:stock-" for i in info["identity"])


def test_infrastructure_summary_is_capped_and_sanitised(tmp_path, registry):
    make_repo(tmp_path, registry, "inventory-svc", {"src/main/java/app/Sender.java": CODE_APP})
    many = "".join(
        f'resource "aws_ecr_repository" "r{i}" {{\n  name = "inventory-svc-{i}"\n}}\n'
        for i in range(25)
    )
    make_repo(tmp_path, registry, "infra-live", {"main.tf": many})
    reply = cross_repo_requests_func(origin="terraform")
    # Nothing is declared as exactly the repo name, so there is no match, and no row is invented.
    assert reply["infrastructure"]["repos"] == {}
    rules = tmp_path / "aliases.json"
    rules.write_text(json.dumps({"prefix:inventory-svc-": "inventory-svc"}))
    import os

    os.environ[sa.ALIASES_FILE_ENV] = str(rules)
    try:
        info = _infra(cross_repo_requests_func(origin="terraform", repos=["inventory-svc"]))
    finally:
        del os.environ[sa.ALIASES_FILE_ENV]
    assert len(info["ecr_repositories"]) == 10
    assert info["counts"]["ecr_repositories"] == 25


def test_awssdk_rows_point_at_the_declaration(infra_world):
    reply = cross_repo_requests_func(origin="awssdk", repos=["inventory-svc"])
    rows = {r["target_name"]: r for r in reply["results"] if r["target_kind"] == "sqs"}
    literal = rows["stock-events"]
    assert literal["status"] == "matched" and literal["target_repo"] == "infra-live"
    assert literal["declared_by"] == {
        "repo": "infra-live", "file": "modules/ecs/main.tf", "line": 13, "name": "stock-events",
    }
    configured = next(r for r in reply["results"] if r.get("matched_profiles"))
    assert configured["matched_profiles"] == ["prod"]
    assert configured["declared_by"]["name"] == "jobs-prod"
    assert configured["declared_by"]["file"] == "modules/ecs/main.tf"
    assert _infra(reply)["counts"]["resources"] == 2


def test_external_targets_show_the_declaring_repo(infra_world, tmp_path):
    result = query_graph(
        "external_targets", "external:aws-sqs", repo_root=str(tmp_path / "inventory-svc"),
    )
    rows = {r["target"]: r for r in result["results"]}
    literal = rows["external:aws-sqs:stock-events"]
    assert literal["matched"] is True
    assert literal["declared_by"]["repo"] == "infra-live"
    assert literal["declared_by"]["file"] == "modules/ecs/main.tf"
    unresolved = next(r for r in result["results"] if r["unresolved"] and r.get("names"))
    assert unresolved["declared_by"]["file"] == "modules/ecs/main.tf"
    own = query_graph(
        "external_targets", "external:aws-sqs", repo_root=str(tmp_path / "infra-live"),
    )
    declared = {r["target"]: r["declared_by"] for r in own["results"] if "declared_by" in r}
    assert declared["external:aws-sqs:jobs-prod"]["repo"] == "infra-live"
    assert declared["external:aws-sqs:jobs-prod"]["line"] == 17


def test_stepfunction_origin_carries_the_summary_too(infra_world):
    reply = cross_repo_requests_func(origin="stepfunction", repos=["inventory-svc"])
    assert reply["status"] == "ok"
    assert "infrastructure" in reply
    assert _infra(reply)["ecr_repositories"]

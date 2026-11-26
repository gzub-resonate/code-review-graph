"""``origin="external"`` and ``origin="consumers"`` of ``cross_repo_requests``.

Everything is synthetic: repo, host, queue and parameter names are made up. Repos are parsed
from real files, so the Endpoint nodes and AWS edges are the ones a build writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import consumer_join as cj
from code_review_graph import external_hosts as eh
from code_review_graph.paging import CursorError
from code_review_graph.parser import NodeInfo
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_service_aliases_infra_join import _server, make_repo
from .test_service_aliases_infra_join import registry as registry  # noqa: F401


def feign(root: Path, line: int, service: str | None, route: str = "/v1/x", **extra) -> NodeInfo:
    meta = {
        "role": "client", "client": "feign", "http_method": "GET", "route": route,
        "config_keys": [], "route_dynamic": False, "url_literal": False,
    }
    if service is not None:
        meta["service"] = service
    meta.update(extra)
    return NodeInfo(
        kind="Endpoint", name=f"c{line}@GetMapping[0:0] GET {route}",
        file_path=str(root / "Client.java"), line_start=line, line_end=line,
        language="java", parent_name="Client", extra=meta,
    )


JS_CLIENT = """\
import axios from 'axios';

export async function sync() {
  await fetch('https://Graph.Facebook.com:443/v19.0/act_1/ads?access_token=SECRET&x=1');
  await axios.get('https://user:pw@tenant-a.auth0.com/api/v2/users');
  await axios.get('https://tenant-b.auth0.com/api/v2/roles');
  await axios.post('https://hooks.example-vendor.io/notify', {});
  await axios.get('https://${BILLING_HOST}/invoices');
  await axios.get('/local/api');
}
"""

TERRAFORM = """\
resource "aws_sqs_queue" "orders" {
  name = "orders-intake-queue"
}

resource "aws_s3_bucket" "exports" {
  bucket = "acme-export-files"
}

resource "aws_ssm_parameter" "db_pass" {
  name  = "/prod/orders/db/password"
  type  = "SecureString"
  tags = {
    name = "/not/the/name"
  }
}

resource "aws_ssm_parameter" "templated" {
  name  = "/${var.env}/orders/feature-flags"
  type  = "String"
}

resource "aws_ssm_parameter" "computed" {
  name  = var.some_name
  type  = "String"
}
"""

CONSUMER_PY = """\
import boto3

sqs = boto3.client("sqs")
ssm = boto3.client("ssm")
s3 = boto3.client("s3")


def run():
    sqs.send_message(
        QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/orders-intake-queue",
        MessageBody="m",
    )
    ssm.get_parameter(Name="/prod/orders/db/password")
    ssm.get_parameter(Name="/dev/orders/feature-flags")
    ssm.get_parameter(Name="/prod/billing/api-key")
    ssm.get_parameters_by_path(Path="/prod/orders")
    ssm.get_parameters_by_path(Path="/prod/reports")
    s3.put_object(Bucket="acme-export-files", Key="a")
"""

OTHER_PY = """\
import boto3

ssm = boto3.client("ssm")


def run():
    ssm.get_parameter(Name="prod/orders/db/password")
    ssm.get_parameter(Name="/prod/other/secret-thing")
"""


@pytest.fixture
def world(tmp_path, registry):  # noqa: F811
    web = tmp_path / "web-app"
    make_repo(tmp_path, registry, "web-app", {"src/sync.js": JS_CLIENT})
    api_root = (tmp_path / "billing-api").resolve()
    make_repo(
        tmp_path, registry, "billing-api",
        nodes=(
            feign(api_root, 3, "auth0-client"),
            feign(api_root, 5, "salesforce-gateway", "/services/data"),
            feign(api_root, 7, "ledger-svc", "/entries"),
            feign(api_root, 9, "${vendor.url}", "/p", service_unresolved=True,
                  config_keys=["vendor.url", "vendor.path"]),
            feign(api_root, 11, "mystery-partner", "/q"),
        ),
    )
    ledger_root = (tmp_path / "ledger-svc").resolve()
    make_repo(tmp_path, registry, "ledger-svc", nodes=(_server(ledger_root, 4, "GET", "/entries"),))
    make_repo(tmp_path, registry, "infra", {"main.tf": TERRAFORM})
    make_repo(tmp_path, registry, "orders-worker", {"worker.py": CONSUMER_PY})
    make_repo(tmp_path, registry, "reports-job", {"job.py": OTHER_PY})
    return web


class TestHostRules:
    def test_split_url_strips_everything_but_the_host(self):
        got = eh.split_url("https://user:pw@WWW.Example.COM:8443/a/b?token=1#f")
        assert got == eh.Host("example.com", "/a/b")
        assert eh.split_url("/relative") is None

    def test_placeholder_host_gives_the_key(self):
        got = eh.split_url("https://${API_HOST:fallback}/x")
        assert got == eh.Host(None, "/x", "API_HOST")

    def test_vendor_table_and_collapse(self):
        assert eh.vendor_of_host("graph.facebook.com") == ("Meta", "graph.facebook.com")
        assert eh.vendor_of_host("acme.eu.auth0.com") == ("Auth0", "*.auth0.com")
        assert eh.vendor_of_host("hooks.example-vendor.io") == (None, "hooks.example-vendor.io")
        assert eh.vendor_of_service("salesforce-gateway") == "Salesforce"
        assert eh.vendor_of_service("ledger-svc") is None


class TestExternal:
    def reply(self, **kw):
        return cross_repo_requests_func(origin="external", max_results=100, **kw)

    def test_groups_by_host_with_vendor_and_never_leaks_secrets(self, world):
        reply = self.reply()
        text = repr(reply)
        assert "SECRET" not in text and "pw@" not in text and "access_token" not in text
        assert ":443" not in text
        rows = {(r["host"], r["repo"]): r for r in reply["results"]}
        meta = rows[("graph.facebook.com", "web-app")]
        assert meta["vendor"] == "Meta" and meta["status"] == "known_vendor"
        assert meta["calls"] == 1 and meta["files"] == ["src/sync.js"]
        assert meta["samples"][0]["path"] == "/v19.0/act_1/ads"
        auth0 = rows[("*.auth0.com", "web-app")]
        assert auth0["calls"] == 2 and sorted(auth0["hosts_seen"]) == [
            "tenant-a.auth0.com", "tenant-b.auth0.com",
        ]
        other = rows[("hooks.example-vendor.io", "web-app")]
        assert other["status"] == "unknown_vendor" and "vendor" not in other

    def test_unresolved_hosts_carry_their_key(self, world):
        reply = self.reply(status="host_unresolved")
        keys = {(r["repo"], r["unresolved_key"]) for r in reply["results"]}
        assert ("web-app", "BILLING_HOST") in keys
        assert ("billing-api", "vendor.url") in keys

    def test_feign_registered_service_is_not_external(self, world):
        hosts = {r["host"] for r in self.reply()["results"]}
        assert "service:ledger-svc" not in hosts
        assert "service:auth0-client" in hosts
        reply = self.reply(service="salesforce")
        assert [r["host"] for r in reply["results"]] == ["service:salesforce-gateway"]
        assert reply["results"][0]["vendor"] == "Salesforce"
        assert reply["results"][0]["vendor_basis"] == "service_name"
        assert any(
            r["host"] == "service:mystery-partner" and r["status"] == "unknown_vendor"
            for r in self.reply()["results"]
        )

    def test_filters(self, world):
        assert {r["host"] for r in self.reply(service="auth0.com")["results"]} == {"*.auth0.com"}
        assert {r["host"] for r in self.reply(service="Meta")["results"]} == {"graph.facebook.com"}
        only = self.reply(repos=["web-app"], route="/api/v2")
        assert {r["host"] for r in only["results"]} == {"*.auth0.com"}
        with pytest.raises(ValueError):
            self.reply(status="bogus")

    def test_pages(self, world):
        everything = self.reply()
        assert everything["total"] >= 5
        rows, cursor = [], None
        while True:
            page = cross_repo_requests_func(origin="external", max_results=2, cursor=cursor)
            rows.extend(page["results"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        assert rows == everything["results"]
        first = cross_repo_requests_func(origin="external", max_results=2)
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="external", max_results=2, service="auth0.com",
                cursor=first["next_cursor"],
            )


class TestConsumers:
    def reply(self, name=None, **kw):
        return cross_repo_requests_func(origin="consumers", service=name, max_results=100, **kw)

    def test_queue_declaration_and_consumer(self, world):
        reply = self.reply("orders-intake-queue")
        rows = reply["results"]
        decl = next(r for r in rows if r["role"] == "declaration")
        assert decl["repo"] == "infra" and decl["file"] == "main.tf" and decl["line"] == 1
        assert decl["service"] == "sqs" and decl["terraform_type"] == "aws_sqs_queue"
        users = [r for r in rows if r["role"] == "consumer"]
        assert users and all(r["repo"] == "orders-worker" and r["match"] == "exact"
                             for r in users)
        assert {r["operation"] for r in users} >= {"send_message"}

    def test_bucket(self, world):
        rows = self.reply("acme-export-files")["results"]
        assert {(r["role"], r["repo"]) for r in rows} == {
            ("declaration", "infra"), ("consumer", "orders-worker"),
        }

    def test_ssm_exact_prefix_and_leading_slash(self, world):
        rows = self.reply("/prod/orders/db/password")["results"]
        decl = [r for r in rows if r["role"] == "declaration"]
        # The literal name is found; the ``name`` inside ``tags`` is not taken for it.
        assert [(d["repo"], d["line"]) for d in decl] == [("infra", 9)]
        by = {(r["repo"], r["match"]) for r in rows if r["role"] == "consumer"}
        assert ("orders-worker", "exact") in by and ("orders-worker", "prefix") in by
        # Another repo reads it without the leading slash: still the same parameter.
        assert ("reports-job", "exact") in by
        prefix = next(r for r in rows if r.get("match") == "prefix")
        assert prefix["path"] == "/prod/orders"
        # A path that does not cover the parameter is not a consumer.
        assert not any(r.get("path") == "/prod/reports" for r in rows)

    def test_templated_declaration(self, world):
        rows = self.reply("/dev/orders/feature-flags")["results"]
        decl = next(r for r in rows if r["role"] == "declaration")
        assert decl["declared_as"] == "template" and decl["repo"] == "infra"

    def test_filters_and_status(self, world):
        rows = self.reply("/prod/orders/db/password", status="prefix")["results"]
        assert rows and all(r["match"] == "prefix" for r in rows)
        only = self.reply("orders-intake-queue", route="s3")["results"]
        assert only == []
        decl = self.reply("orders-intake-queue", status="declaration")["results"]
        assert [r["role"] for r in decl] == ["declaration"]

    def test_short_and_generic_names_are_refused(self, world):
        for name in ("abc", "logs", "/test/"):
            reply = self.reply(name)
            assert reply["results"] == [] and reply["refused"] in (
                "name_too_short", "name_generic")
        assert cj.refusal("orders-intake-queue") is None

    def test_no_name_lists_undeclared_ssm_reads(self, world):
        reply = self.reply()
        names = {(r["repo"], r["name"]) for r in reply["results"]}
        assert ("orders-worker", "/prod/billing/api-key") in names
        assert ("reports-job", "/prod/other/secret-thing") in names
        assert ("orders-worker", "/prod/reports") in names
        # Declared (literal, slash-less spelling, template) and covered path: not listed.
        assert ("orders-worker", "/prod/orders/db/password") not in names
        assert ("reports-job", "prod/orders/db/password") not in names
        assert ("orders-worker", "/prod/orders") not in names
        assert ("orders-worker", "/dev/orders/feature-flags") not in names
        assert reply["declarations_with_computed_name"] == 1
        assert all(r["role"] == "undeclared_read" for r in reply["results"])

    def test_pages(self, world):
        everything = self.reply()
        rows, cursor = [], None
        while True:
            page = cross_repo_requests_func(
                origin="consumers", max_results=1, cursor=cursor)
            rows.extend(page["results"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        assert rows == everything["results"] and len(rows) >= 3

    def test_repos_limit_consumers_not_declarations(self, world):
        rows = self.reply("acme-export-files", repos=["reports-job"])["results"]
        assert [(r["role"], r["repo"]) for r in rows] == [("declaration", "infra")]

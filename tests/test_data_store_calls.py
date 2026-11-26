"""RDS, load balancer, ElastiCache and OpenSearch calls in the ``awssdk`` join.

Covers the SDK operations (``aws_sdk_calls``), the client connections that name a host
(``data_store_calls``), the Terraform declarations and the exact-name join (``aws_sdk_links``).
Everything runs offline on inline source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import aws_sdk_links as links
from code_review_graph.aws_sdk_links import read_edges
from code_review_graph.data_store_calls import classify_host, parse_targets
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

ROOT = Path("/work/svc")
RDS = "mydb.abc123xyz.us-east-1.rds.amazonaws.com"
CLUSTER = "mycluster.cluster-abc123xyz.us-east-1.rds.amazonaws.com"
CACHE_RG = "mycache.abc123.ng.0001.use1.cache.amazonaws.com"
ES = "search-mydomain-abcdefghijklmnopqrstuvwxyz.us-east-1.es.amazonaws.com"
ALB = "my-alb-1234567890.us-east-1.elb.amazonaws.com"


def edges_of(name: str, source: str) -> list[Any]:
    _nodes, edges = CodeParser().parse_bytes(ROOT / name, source.encode())
    return [e for e in edges if e.kind == "REFERENCES" and e.target.startswith("external:aws-")]


def rows(name: str, source: str) -> set[tuple[str, str]]:
    return {(e.target, e.extra["operation"]) for e in edges_of(name, source)}


# -- SDK operations ----------------------------------------------------------------------------


def test_boto3_rds_and_cluster_operations() -> None:
    src = (
        "import boto3\n"
        "rds = boto3.client('rds')\n"
        "def go():\n"
        "    rds.describe_db_instances(DBInstanceIdentifier='mydb')\n"
        "    rds.create_db_snapshot(DBInstanceIdentifier='mydb', DBSnapshotIdentifier='s')\n"
        "    rds.failover_db_cluster(DBClusterIdentifier='mycluster')\n"
        "    rds.describe_db_instances()\n"
        "    rds.describe_db_engine_versions(Engine='postgres')\n"
    )
    found = {(e.target, e.extra["operation"], e.extra["target_kind"], e.extra["access"])
             for e in edges_of("a.py", src)}
    assert found == {
        ("external:aws-rds:mydb", "describe_db_instances", "db-instance", "read"),
        ("external:aws-rds:mydb", "create_db_snapshot", "db-instance", "admin"),
        ("external:aws-rds:mycluster", "failover_db_cluster", "db-cluster", "write"),
    }


def test_boto3_load_balancer_target_group_cache_and_search_operations() -> None:
    src = (
        "import boto3\n"
        "elb = boto3.client('elbv2')\n"
        "classic = boto3.client('elb')\n"
        "cache = boto3.client('elasticache')\n"
        "es = boto3.client('opensearch')\n"
        "def go(tg):\n"
        "    elb.describe_load_balancers(Names=['alb-a', 'alb-b'])\n"
        "    elb.describe_target_health(\n"
        "        TargetGroupArn='arn:aws:elasticloadbalancing:us-east-1:123456789012:"
        "targetgroup/tg-one/73e2d6bc24d8a067')\n"
        "    elb.describe_load_balancers(LoadBalancerArns=['arn:aws:elasticloadbalancing:"
        "us-east-1:123456789012:loadbalancer/app/alb-c/50dc6c495c0c9188'])\n"
        "    classic.describe_instance_health(LoadBalancerName='clb-d')\n"
        "    cache.describe_cache_clusters(CacheClusterId='cc-1')\n"
        "    cache.describe_replication_groups(ReplicationGroupId='rg-1')\n"
        "    es.describe_domain(DomainName='mydomain')\n"
        "    elb.register_targets(TargetGroupArn=tg, Targets=[])\n"
    )
    found = {(e.target, e.extra["target_kind"]) for e in edges_of("b.py", src)}
    assert found == {
        ("external:aws-elasticloadbalancing:alb-a", "load-balancer"),
        ("external:aws-elasticloadbalancing:alb-b", "load-balancer"),
        ("external:aws-elasticloadbalancing:tg-one", "target-group"),
        ("external:aws-elasticloadbalancing:alb-c", "load-balancer"),
        ("external:aws-elasticloadbalancing:clb-d", "load-balancer"),
        ("external:aws-elasticache:cc-1", "cache-cluster"),
        ("external:aws-elasticache:rg-1", "replication-group"),
        ("external:aws-es:mydomain", "search-domain"),
        ("external:aws-elasticloadbalancing:?", "target-group"),
    }


def test_java_v1_and_v2_requests() -> None:
    v2 = (
        "import software.amazon.awssdk.services.rds.model.DescribeDbClustersRequest;\n"
        "import software.amazon.awssdk.services.elasticloadbalancingv2.model."
        "DescribeLoadBalancersRequest;\n"
        "import software.amazon.awssdk.services.opensearch.model.DescribeDomainRequest;\n"
        "class A { void f() {\n"
        "  DescribeDbClustersRequest.builder().dbClusterIdentifier(\"c1\").build();\n"
        "  DescribeLoadBalancersRequest.builder().names(\"alb-j\").build();\n"
        "  DescribeDomainRequest.builder().domainName(\"dom-j\").build();\n"
        "} }\n"
    )
    assert {t for t, _ in rows("A.java", v2)} == {
        "external:aws-rds:c1", "external:aws-elasticloadbalancing:alb-j",
        "external:aws-es:dom-j",
    }
    v1 = (
        "import com.amazonaws.services.elasticache.model.DescribeReplicationGroupsRequest;\n"
        "import com.amazonaws.services.rds.model.DescribeDBInstancesRequest;\n"
        "class B { void f() {\n"
        "  new DescribeReplicationGroupsRequest().withReplicationGroupId(\"rg-j\");\n"
        "  new DescribeDBInstancesRequest().withDBInstanceIdentifier(\"db-j\");\n"
        "} }\n"
    )
    assert {t for t, _ in rows("B.java", v1)} == {
        "external:aws-elasticache:rg-j", "external:aws-rds:db-j",
    }


def test_js_commands() -> None:
    src = (
        "import { DescribeDBInstancesCommand, RDSClient } from '@aws-sdk/client-rds';\n"
        "import { DescribeLoadBalancersCommand } from "
        "'@aws-sdk/client-elastic-load-balancing-v2';\n"
        "import { DescribeDomainCommand } from '@aws-sdk/client-opensearch';\n"
        "export async function f() {\n"
        "  await c.send(new DescribeDBInstancesCommand({ DBInstanceIdentifier: 'db-js' }));\n"
        "  await c.send(new DescribeLoadBalancersCommand({ Names: ['alb-js'] }));\n"
        "  await c.send(new DescribeDomainCommand({ DomainName: process.env.DOMAIN }));\n"
        "}\n"
    )
    edges = edges_of("a.ts", src)
    assert {e.target for e in edges} == {
        "external:aws-rds:db-js", "external:aws-elasticloadbalancing:alb-js",
        "external:aws-es:?",
    }
    unresolved = next(e for e in edges if e.target == "external:aws-es:?")
    assert unresolved.extra["unresolved"] and unresolved.extra["placeholder"] == "DOMAIN"


# -- Hosts -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (RDS, ("rds", "db-instance", "mydb")),
        (CLUSTER, ("rds", "db-cluster", "mycluster")),
        ("mycluster.cluster-ro-abc.us-east-1.rds.amazonaws.com",
         ("rds", "db-cluster", "mycluster")),
        ("mycluster.cluster-custom-abc.us-east-1.rds.amazonaws.com",
         ("rds", "db-cluster", None)),
        ("myproxy.proxy-abc.us-east-1.rds.amazonaws.com", ("rds", "db-proxy", "myproxy")),
        (CACHE_RG, ("elasticache", "replication-group", "mycache")),
        ("clustercfg.mycache.abc123.use1.cache.amazonaws.com",
         ("elasticache", "replication-group", "mycache")),
        ("mycluster.abc123.0001.use1.cache.amazonaws.com",
         ("elasticache", "cache-cluster", "mycluster")),
        ("name-abc.serverless.use1.cache.amazonaws.com", ("elasticache", "cache-cluster", None)),
        (ES, ("es", "search-domain", "mydomain")),
        ("vpc-my-dom-abcdefghijklmnopqrstuvwxyz.us-east-1.es.amazonaws.com",
         ("es", "search-domain", "my-dom")),
        (ALB, ("elasticloadbalancing", "load-balancer", "my-alb")),
        ("my-nlb-0123456789abcdef.elb.us-east-1.amazonaws.com",
         ("elasticloadbalancing", "load-balancer", "my-nlb")),
        ("internal-my-alb-1234567890.us-east-1.elb.amazonaws.com",
         ("elasticloadbalancing", "load-balancer", None)),
        ("db.prod.corp.internal", ("rds", "db-instance", None)),
    ],
)
def test_host_classification(host: str, expected: tuple[str, str, str | None]) -> None:
    found = classify_host(host)
    assert (found.service, found.kind, found.name) == expected


def test_parse_targets_drops_credentials_and_queries() -> None:
    secret = "s3cr3t"
    for text in (
        f"jdbc:postgresql://app:{secret}@{RDS}:5432/orders?user=u&password={secret}",
        f"jdbc:sqlserver://{RDS}:1433;databaseName=orders;password={secret}",
        f"jdbc:postgresql://{RDS}/orders?password=p@{secret}",
        f"jdbc:mysql://{RDS}/orders?user=a&password=b/{secret}",
    ):
        found = parse_targets(text, "sql")
        assert [t.host for t in found] == [RDS]
        assert found[0].database == "orders"
        assert secret not in repr(found)
    redis = parse_targets(f"rediss://:{secret}@{CACHE_RG}:6379/0", "redis")
    assert redis[0].host == CACHE_RG and redis[0].port == 6379 and secret not in repr(redis)


# -- Connections ---------------------------------------------------------------------------


def test_jdbc_urls_in_java_and_placeholders() -> None:
    src = (
        "class A {\n"
        f'  static final String URL = "jdbc:postgresql://app:pw123@{RDS}:5432/orders";\n'
        '  static final String ENV = "jdbc:mysql://${DB_HOST:localhost}:3306/x";\n'
        f'  static final String C = "jdbc:postgresql://{CLUSTER}/reports";\n'
        '  static final String L = "jdbc:postgresql://localhost:5432/dev";\n'
        "}\n"
    )
    edges = edges_of("A.java", src)
    by = {(e.target, e.extra.get("placeholder")): e for e in edges}
    assert set(by) == {
        ("external:aws-rds:mydb", None),
        ("external:aws-rds:?", "DB_HOST"),
        ("external:aws-rds:mycluster", None),
    }
    assert by[("external:aws-rds:mydb", None)].extra["database"] == "orders"
    assert by[("external:aws-rds:mycluster", None)].extra["target_kind"] == "db-cluster"
    assert "pw123" not in json.dumps([e.extra for e in edges])


def test_spring_config_files() -> None:
    yml = (
        "spring:\n"
        "  datasource:\n"
        f"    url: jdbc:postgresql://{RDS}:5432/orders?password=hunter2\n"
        "  data:\n"
        "    redis:\n"
        "      host: ${REDIS_HOST:localhost}\n"
        "  elasticsearch:\n"
        "    uris:\n"
        f"      - https://{ES}:443\n"
    )
    edges = edges_of("src/main/resources/application.yml", yml)
    assert {(e.target, e.extra.get("placeholder")) for e in edges} == {
        ("external:aws-rds:mydb", None),
        ("external:aws-elasticache:?", "REDIS_HOST"),
        ("external:aws-es:mydomain", None),
    }
    assert "hunter2" not in json.dumps([e.extra for e in edges])
    props = (
        "spring.datasource.url=jdbc:mysql://${db.host}:3306/app\n"
        f"spring.data.redis.cluster.nodes={CACHE_RG}:6379\n"
    )
    found = {
        (e.target, e.extra.get("placeholder"))
        for e in edges_of("src/main/resources/application-prod.properties", props)
    }
    assert found == {
        ("external:aws-rds:?", "db.host"), ("external:aws-elasticache:mycache", None),
    }
    # Not a Spring configuration file name: nothing is read.
    assert edges_of("docker-compose.yml", yml) == []


def test_python_clients() -> None:
    src = (
        "import os\n"
        "import psycopg2\n"
        "import redis\n"
        "from elasticsearch import Elasticsearch\n"
        "from sqlalchemy import create_engine\n"
        "def go():\n"
        f"    psycopg2.connect(host='{RDS}', dbname='orders', password='pw9')\n"
        "    psycopg2.connect(host=os.environ['PGHOST'])\n"
        f"    redis.Redis(host='{CACHE_RG}', port=6379)\n"
        f"    redis.from_url('rediss://:pw9@{CACHE_RG}:6379/0')\n"
        f"    Elasticsearch(hosts=[{{'host': '{ES}', 'port': 443}}])\n"
        f"    create_engine('postgresql+psycopg2://u:pw9@{CLUSTER}:5432/db')\n"
        "    redis.Redis()\n"
        "    psycopg2.connect(**cfg)\n"
    )
    edges = edges_of("a.py", src)
    assert {(e.target, e.extra.get("placeholder")) for e in edges} == {
        ("external:aws-rds:mydb", None), ("external:aws-rds:?", "PGHOST"),
        ("external:aws-elasticache:mycache", None), ("external:aws-es:mydomain", None),
        ("external:aws-rds:mycluster", None),
    }
    assert "pw9" not in json.dumps([e.extra for e in edges])


def test_a_client_that_is_not_imported_from_the_library_makes_no_edge() -> None:
    src = (
        "class redis:\n"
        "    def Redis(self, host): ...\n"
        "def go(redis):\n"
        "    redis.Redis(host='cache.corp.internal')\n"
    )
    assert edges_of("a.py", src) == []


def test_java_clients() -> None:
    src = (
        "import redis.clients.jedis.JedisPool;\n"
        "import io.lettuce.core.RedisURI;\n"
        "import org.elasticsearch.client.RestClient;\n"
        "import org.apache.http.HttpHost;\n"
        "class A { void f() {\n"
        f'  new JedisPool("{CACHE_RG}", 6379);\n'
        f'  RedisURI.create("rediss://:pw1@{CACHE_RG}:6379");\n'
        f'  RestClient.builder(new HttpHost("{ES}", 443, "https"));\n'
        '  new HttpHost(System.getenv("ES_HOST"), 9200);\n'
        "} }\n"
    )
    edges = edges_of("A.java", src)
    assert {(e.target, e.extra.get("placeholder")) for e in edges} == {
        ("external:aws-elasticache:mycache", None), ("external:aws-es:mydomain", None),
        ("external:aws-es:?", "ES_HOST"),
    }
    assert "pw1" not in json.dumps([e.extra for e in edges])
    # HttpHost with no Elasticsearch/OpenSearch client imported is an ordinary HTTP host.
    plain = (
        "import org.apache.http.HttpHost;\n"
        f'class B {{ void f() {{ new HttpHost("{ES}", 443); }} }}\n'
    )
    assert {e.extra["sdk"] for e in edges_of("B.java", plain)} == {"endpoint-literal"}


def test_js_clients() -> None:
    src = (
        "import Redis from 'ioredis';\n"
        "import { Pool } from 'pg';\n"
        "import { Client } from '@opensearch-project/opensearch';\n"
        "const { createClient } = require('redis');\n"
        "export function f() {\n"
        f"  new Redis({{ host: '{CACHE_RG}', port: 6379 }});\n"
        "  new Pool({ host: process.env.PGHOST, database: 'orders' });\n"
        f"  new Client({{ node: 'https://user:pw2@{ES}' }});\n"
        f"  createClient({{ url: 'rediss://:pw2@{CACHE_RG}:6379' }});\n"
        "  new Pool(config);\n"
        "}\n"
    )
    edges = edges_of("a.ts", src)
    assert {(e.target, e.extra.get("placeholder")) for e in edges} == {
        ("external:aws-elasticache:mycache", None), ("external:aws-rds:?", "PGHOST"),
        ("external:aws-es:mydomain", None),
    }
    assert "pw2" not in json.dumps([e.extra for e in edges])


def test_endpoint_literal_and_loopback_and_comments() -> None:
    src = (
        f'HOST = "{ALB}"\n'
        f"# old: {RDS}\n"
        'LOCAL = "redis://localhost:6379/0"\n'
    )
    edges = edges_of("a.py", src)
    assert {e.target for e in edges} == {"external:aws-elasticloadbalancing:my-alb"}


def test_test_code_makes_no_edge() -> None:
    src = f'URL = "jdbc:postgresql://{RDS}/x"\n'
    assert edges_of("tests/test_x.py", src) == []
    java = f'class A {{ String u = "jdbc:mysql://{RDS}/x"; }}'
    _nodes, found = CodeParser(ROOT).parse_bytes(ROOT / "src/test/java/ATest.java", java.encode())
    assert [e for e in found if e.target.startswith("external:aws-")] == []


# -- Terraform and the join --------------------------------------------------------------------


TF = '''
resource "aws_db_instance" "main" {
  identifier = "mydb"
}
resource "aws_rds_cluster" "agg" {
  cluster_identifier = "mycluster"
}
resource "aws_elasticache_replication_group" "c" {
  replication_group_id = "mycache"
}
resource "aws_elasticache_cluster" "single" {
  cluster_id = "solo"
}
resource "aws_lb" "front" {
  name = "my-alb"
}
resource "aws_lb_target_group" "tg" {
  name = "tg-one"
}
resource "aws_opensearch_domain" "search" {
  domain_name = "mydomain"
}
resource "aws_db_instance" "dyn" {
  identifier = "${var.env}-db"
}
'''


def _store(tmp_path: Path, name: str, files: dict[str, str]) -> GraphStore:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    parser = CodeParser(repo)
    store = GraphStore(str(tmp_path / f"{name}.db"))
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return store


def test_terraform_declares_data_stores(tmp_path: Path) -> None:
    store = _store(tmp_path, "infra", {"main.tf": TF})
    try:
        declared = {
            (e["service"], e["kind"], e["name"])
            for e in read_edges(store) if e["operation"] == "declare"
        }
    finally:
        store.close()
    assert declared == {
        ("rds", "db-instance", "mydb"), ("rds", "db-cluster", "mycluster"),
        ("elasticache", "replication-group", "mycache"), ("elasticache", "cache-cluster", "solo"),
        ("elasticloadbalancing", "load-balancer", "my-alb"),
        ("elasticloadbalancing", "target-group", "tg-one"),
        ("es", "search-domain", "mydomain"),
    }


APP = (
    "import boto3, psycopg2, redis\n"
    "rds = boto3.client('rds')\n"
    "elb = boto3.client('elbv2')\n"
    "def go():\n"
    f"    psycopg2.connect(host='{RDS}')\n"
    f"    psycopg2.connect(host='{CLUSTER}')\n"
    f"    redis.Redis(host='{CACHE_RG}')\n"
    "    redis.Redis(host='mycache-001.abc123.0001.use1.cache.amazonaws.com')\n"
    "    rds.describe_db_clusters(DBClusterIdentifier='mycache')\n"
    "    elb.describe_load_balancers(Names=['my-alb'])\n"
    "    elb.describe_target_health(TargetGroupArn='arn:aws:elasticloadbalancing:us-east-1:1:"
    "targetgroup/my-alb/0123456789abcdef')\n"
    f"    psycopg2.connect(host='internal-my-alb-1234567890.us-east-1.elb.amazonaws.com')\n"
    "    psycopg2.connect(host='db.prod.corp.internal')\n"
)


def test_join_matches_by_exact_name_and_kind(tmp_path: Path) -> None:
    infra = _store(tmp_path, "infra", {"main.tf": TF})
    app = _store(tmp_path, "app", {"app.py": APP})
    try:
        infra_edges = read_edges(infra)
        app_edges = read_edges(app)
    finally:
        infra.close()
        app.close()
    repos = [{"path": str(tmp_path / "infra"), "alias": "infra"},
             {"path": str(tmp_path / "app"), "alias": "app"}]
    result = links.resolve_links({0: infra_edges, 1: app_edges}, repos, lambda i, t: False)
    app_rows = [r for r in result if r["repo"] == "app"]

    def row(target: str, kind: str) -> dict[str, Any]:
        return next(r for r in app_rows if r["target"] == target and r["resource_kind"] == kind)

    assert row("rds/mydb", "db-instance")["status"] == "matched"
    assert row("rds/mydb", "db-instance")["target_repo"] == "infra"
    assert row("rds/mydb", "db-instance")["declared_by"]["file"] == "main.tf"
    assert row("rds/mycluster", "db-cluster")["status"] == "matched"
    assert row("elasticache/mycache", "replication-group")["status"] == "matched"
    # A cluster call named like a replication group, and a node host, do not match it.
    assert row("rds/mycache", "db-cluster")["status"] == "not_found"
    assert row("elasticache/mycache-001", "cache-cluster")["status"] == "not_found"
    assert row("elasticloadbalancing/my-alb", "load-balancer")["status"] == "matched"
    # The same name as a target group declared elsewhere is not a load balancer match.
    assert row("elasticloadbalancing/my-alb", "target-group")["status"] == "not_found"
    unresolved = [r for r in app_rows if r["status"] == "unresolved"]
    assert {r.get("unresolved_reason") for r in unresolved} == {
        "internal_prefix", "non_aws_host",
    }
    assert all(r["target_name"] == "?" for r in unresolved)
    assert next(r for r in unresolved if r["unresolved_reason"] == "non_aws_host")[
        "host"] == "db.prod.corp.internal"


def test_sdk_and_connection_rows_carry_no_credentials(tmp_path: Path) -> None:
    src = f'URL = "jdbc:postgresql://admin:topsecret@{RDS}/orders?password=topsecret"\n'
    store = _store(tmp_path, "app", {"A.py": src})
    try:
        dumped = json.dumps(read_edges(store))
    finally:
        store.close()
    assert "topsecret" not in dumped and "admin" not in dumped and "mydb" in dumped

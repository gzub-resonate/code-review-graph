# ruff: noqa: E501
"""A literal AWS endpoint host is evidence of a data store only with a context around it.

A host in a comment, docstring, sample config, test fixture, documentation or log message makes no
row. A host in a client call or under a config key (``spring.datasource.url``, ``DB_HOST``,
``*_ENDPOINT``) does. A host that survives with neither is marked ``evidence: hostname_only`` with a
lower confidence. Credentials never reach any output field.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_review_graph.aws_sdk_calls import _Out, _Scopes
from code_review_graph.data_store_calls import scan_config, skip_path
from code_review_graph.parser import CodeParser

ROOT = Path("/work/svc")
RDS = "mydb.abc123xyz.us-east-1.rds.amazonaws.com"
CACHE = "mycache.abc123.ng.0001.use1.cache.amazonaws.com"
ES = "search-mydomain-abcdefghijklmnopqrstuvwxyz.us-east-1.es.amazonaws.com"
ALB = "my-alb-1234567890.us-east-1.elb.amazonaws.com"
AOS = "mydomain.us-east-1.aos.us-east-1.on.aws"


#: ``.env`` and ``.tfvars`` files have no language, so the parser never indexes them: their
#: scanner is driven directly. Every other file goes through ``CodeParser``.
_UNPARSED = (".env", ".tfvars")


def _direct(name: str, source: str) -> list[Any]:
    if skip_path(ROOT / name, ROOT):
        return []
    out = _Out(_Scopes([], [], str(ROOT / name)), lambda **kw: SimpleNamespace(**kw))
    scan_config(Path(name).name, source, out)
    return out.edges


def edges(name: str, source: str) -> list[Any]:
    if Path(name).name.lower().endswith(_UNPARSED) or ".env" in Path(name).name.lower():
        found = _direct(name, source)
    else:
        _nodes, found = CodeParser(ROOT).parse_bytes(ROOT / name, source.encode())
    return [
        e for e in found if e.kind == "REFERENCES" and e.target.startswith("external:aws-")
        and e.extra.get("sdk") != "terraform"
    ]


# -- False positives: nothing is a data-store dependency ------------------------------------------

COMMENTED = [
    ("a.py", f"# connect to {RDS}\nx = 1\n"),
    ("a.py", f"x = 1  # was {RDS}\n"),
    ("a.py", f'def f():\n    """Talks to {RDS} in prod."""\n    return 1\n'),
    ("a.py", f"'''\nexample:\n    host = '{RDS}'\n'''\n"),
    ("A.java", f"class A {{\n  /* old host {RDS}\n     {CACHE} */\n  int x;\n}}\n"),
    ("A.java", f"/**\n * Uses {RDS}.\n */\nclass A {{}}\n"),
    ("A.java", f"class A {{ int x; // {RDS}\n}}\n"),
    ("a.ts", f"// {ALB}\nexport const x = 1;\n"),
    ("a.ts", f"export const x = 1; // {ALB}\n"),
    ("a.js", f"/*\n{ES}\n*/\nmodule.exports = 1;\n"),
    ("src/main/resources/application.yml", f"# host: {RDS}\nserver:\n  port: 1\n"),
    ("src/main/resources/application.yml", f"server:\n  port: 1 # {RDS}\n"),
    ("src/main/resources/application.properties", f"# db={RDS}\nserver.port=1\n"),
]
LOGGED = [
    ("a.py", f'import logging\nlog = logging.getLogger()\nlog.info("connecting to {RDS}")\n'),
    ("a.py", f'print("cache is {CACHE}")\n'),
    ("A.java", f'class A {{ void f() {{ log.warn("slow {RDS}"); }} }}'),
    ("a.ts", f"console.log('hit {ALB}');\n"),
    ("a.py", f'raise RuntimeError("could not reach {RDS}")\n'),
]
SAMPLES = [
    ("src/main/resources/application-sample.yml", f"spring:\n  datasource:\n    url: jdbc:postgresql://{RDS}/x\n"),
    ("config/application.example.properties", f"spring.datasource.url=jdbc:mysql://{RDS}/x\n"),
    (".env.example", f"DB_HOST={RDS}\n"),
    ("config/.env.sample", f"REDIS_URL=redis://{CACHE}:6379/0\n"),
    ("config/settings.dist.yml", f"DB_HOST: {RDS}\n"),
    ("docker-compose.example.yml",
     f"services:\n  app:\n    environment:\n      DB_HOST: {RDS}\n"),
    ("terraform/prod.tfvars.example", f'db_host = "{RDS}"\n'),
]
TESTS_AND_FIXTURES = [
    ("tests/test_a.py", f'DB_HOST = "{RDS}"\n'),
    ("src/__tests__/a.ts", f"export const DB_HOST = '{RDS}';\n"),
    ("src/a.spec.ts", f"export const DB_HOST = '{RDS}';\n"),
    ("src/a.test.js", f"const DB_HOST = '{RDS}';\n"),
    ("src/test/java/ATest.java", f'class ATest {{ String DB_HOST = "{RDS}"; }}'),
    ("fixtures/app.py", f'DB_HOST = "{RDS}"\n'),
    ("src/fixtures/application.yml", f"spring:\n  datasource:\n    url: jdbc:postgresql://{RDS}/x\n"),
    ("test/fixtures/.env", f"DB_HOST={RDS}\n"),
    ("src/mocks/db.ts", f"export const DB_HOST = '{RDS}';\n"),
    ("src/__mocks__/db.js", f"module.exports = {{ DB_HOST: '{RDS}' }};\n"),
    ("testdata/application.yml", f"spring:\n  datasource:\n    url: jdbc:postgresql://{RDS}/x\n"),
    ("examples/app.py", f'DB_HOST = "{RDS}"\n'),
    ("docs/snippet.py", f'DB_HOST = "{RDS}"\n'),
    ("fixtures/main.tf", f'variable "db_host" {{\n  default = "{RDS}"\n}}\n'),
    ("fixtures/docker-compose.yml",
     f"services:\n  app:\n    environment:\n      - DB_HOST={RDS}\n"),
]
DOCS_AND_TF_TEXT = [
    ("README.md", f"```python\nDB_HOST = '{RDS}'\n```\n"),
    ("docs/setup.md", f"Set DB_HOST to {RDS}\n"),
    ("notes.txt", f"DB_HOST={RDS}\n"),
    ("outputs.tf",
     f'output "db" {{\n  description = "Reachable at {RDS}"\n  value = aws_db_instance.a.address\n}}\n'),
    ("main.tf", f'# endpoint is {RDS}\nresource "aws_db_instance" "a" {{\n  identifier = "a"\n}}\n'),
    ("variables.tf",
     f'variable "db_host" {{\n  description = "e.g. {RDS}"\n  type = string\n}}\n'),
]
CONFIG_NOISE = [
    ("src/main/resources/application.yml",
     f"app:\n  description: see {RDS} for details\n  banner: 'cache {CACHE}'\n"),
    (".env", f"NOTE=ask about {RDS}\n# DB_HOST={RDS}\n"),
    ("docker-compose.yml",
     f"services:\n  app:\n    labels:\n      description: {RDS}\n"),
    ("prod.tfvars", f'description = "{RDS}"\n# db_host = "{RDS}"\n'),
]


@pytest.mark.parametrize(
    "name,source",
    COMMENTED + LOGGED + SAMPLES + TESTS_AND_FIXTURES + DOCS_AND_TF_TEXT + CONFIG_NOISE,
    ids=lambda v: v if isinstance(v, str) and len(v) < 60 and "\n" not in v else "src",
)
def test_no_row_without_a_real_context(name: str, source: str) -> None:
    assert edges(name, source) == [], name


# -- True positives -------------------------------------------------------------------------------


def one(name: str, source: str) -> dict[str, Any]:
    found = edges(name, source)
    assert len(found) == 1, (name, [e.extra for e in found])
    return found[0].extra


def test_application_yml_and_properties() -> None:
    yml = f"spring:\n  datasource:\n    url: jdbc:postgresql://{RDS}:5432/orders\n"
    extra = one("src/main/resources/application.yml", yml)
    assert extra["resource"] == "mydb" and extra["confidence"] == 1.0
    assert extra.get("evidence") != "hostname_only"
    props = f"spring.data.redis.host={CACHE}\n"
    assert one("src/main/resources/application.properties", props)["resource"] == "mycache"
    keyed = f"app:\n  search:\n    endpoint: https://{ES}\n"
    assert one("src/main/resources/application.yml", keyed)["resource"] == "mydomain"


def test_env_file_and_compose_and_tfvars_and_tf_default() -> None:
    assert one(".env", f"DB_HOST={RDS}\n")["resource"] == "mydb"
    assert one("config/prod.env", f'export REDIS_URL="redis://{CACHE}:6379"\n')["resource"] \
        == "mycache"
    assert one(".env", f"SEARCH_ENDPOINT=https://{ES}\n")["resource"] == "mydomain"
    compose_map = f"services:\n  app:\n    environment:\n      DB_HOST: {RDS}\n"
    assert one("docker-compose.yml", compose_map)["resource"] == "mydb"
    compose_list = f"services:\n  app:\n    environment:\n      - DB_HOST={RDS}\n"
    assert one("compose.yaml", compose_list)["resource"] == "mydb"
    assert one("infra/prod.tfvars", f'db_host = "{RDS}"\n')["resource"] == "mydb"
    tf = f'variable "db_host" {{\n  type = string\n  default = "{RDS}"\n}}\n'
    assert one("infra/variables.tf", tf)["resource"] == "mydb"


def test_hosts_in_strings_passed_to_known_clients() -> None:
    py = f'import psycopg2\nc = psycopg2.connect(host="{RDS}", dbname="x")\n'
    assert one("a.py", py)["resource"] == "mydb"
    py = f'import redis\nr = redis.Redis(host="{CACHE}")\n'
    assert one("a.py", py)["resource"] == "mycache"
    java = (
        "import redis.clients.jedis.Jedis;\n"
        f'class A {{ Object j = new Jedis("{CACHE}", 6379); }}\n'
    )
    assert one("A.java", java)["resource"] == "mycache"
    js = f"const {{ Client }} = require('pg');\nconst c = new Client({{ host: '{RDS}' }});\n"
    assert one("a.js", js)["resource"] == "mydb"


def test_endpoint_under_a_host_named_variable_or_env_default() -> None:
    assert one("a.py", f'DB_HOST = "{RDS}"\n')["resource"] == "mydb"
    env = f'import os\nh = os.environ.get("CACHE_HOST", "{CACHE}")\n'
    assert one("a.py", env)["resource"] == "mycache"
    assert one("a.ts", f"export const SEARCH_ENDPOINT = '{ES}';\n")["resource"] == "mydomain"


def test_bare_hostname_survives_only_as_hostname_only_with_lower_confidence() -> None:
    extra = one("a.py", f'targets = ["{RDS}"]\n')
    assert extra["evidence"] == "hostname_only"
    assert extra["confidence"] < 0.6 and extra["confidence_tier"] == "INFERRED"
    assert extra["resource"] == "mydb"
    # a string next to no key and no client in Java
    extra = one("A.java", f'class A {{ String s = pick("{ALB}"); }}')
    assert extra["evidence"] == "hostname_only"


def test_every_other_row_names_its_evidence() -> None:
    assert one("a.py", f'DB_HOST = "{RDS}"\n')["evidence"] == "config_key"
    py = f'import psycopg2\nc = psycopg2.connect(host="{RDS}")\n'
    assert one("a.py", py)["evidence"] == "client_call"
    assert one("a.py", f'U = "jdbc:postgresql://{RDS}/x"\n')["evidence"] == "connection_url"


# -- Credentials ----------------------------------------------------------------------------------

SECRET = "s3cr3tPw"


@pytest.mark.parametrize(
    "name,source",
    [
        ("A.java", f'class A {{ String u = "jdbc:postgresql://app:{SECRET}@{RDS}:5432/o"; }}'),
        ("A.java", f'class A {{ String u = "jdbc:postgresql://{RDS}/o?user=a&password={SECRET}"; }}'),
        ("A.java", f'class A {{ String u = "jdbc:sqlserver://{RDS}:1433;databaseName=o;password={SECRET}"; }}'),
        ("A.java", f'class A {{ String u = "jdbc:mysql://a:{SECRET}@{RDS}/o?password={SECRET}"; }}'),
        ("a.py", f'U = "redis://default:{SECRET}@{CACHE}:6379/0"\n'),
        ("a.py", f'U = "rediss://:{SECRET}@{CACHE}:6379/0?password={SECRET}"\n'),
        ("a.py", f'import redis\nr = redis.from_url("redis://u:{SECRET}@{CACHE}:6379/0")\n'),
        ("a.py", f'import sqlalchemy\ne = sqlalchemy.create_engine("postgresql://u:{SECRET}@{RDS}/d")\n'),
        ("a.py", f'import psycopg2\nc = psycopg2.connect("host={RDS} dbname=d password={SECRET} user=u")\n'),
        ("a.ts", f"const {{ Client }} = require('pg');\nnew Client({{ connectionString: 'postgres://u:{SECRET}@{RDS}/d' }});\n"),
        ("src/main/resources/application.yml",
         f"spring:\n  datasource:\n    url: jdbc:postgresql://{RDS}/d?password={SECRET}\n    password: {SECRET}\n"),
        ("src/main/resources/application.properties",
         f"spring.datasource.url=jdbc:postgresql://u:{SECRET}@{RDS}/d\nspring.data.redis.password={SECRET}\n"),
        (".env", f"DB_URL=postgres://u:{SECRET}@{RDS}/d\nDB_PASSWORD={SECRET}\n"),
        ("docker-compose.yml",
         f"services:\n  a:\n    environment:\n      - REDIS_URL=redis://:{SECRET}@{CACHE}:6379\n"),
        ("infra/prod.tfvars", f'db_url = "postgres://u:{SECRET}@{RDS}/d"\n'),
    ],
)
def test_no_output_field_holds_a_credential(name: str, source: str) -> None:
    found = edges(name, source)
    assert found, name
    dumped = json.dumps(
        [{"t": e.target, "x": e.extra, "s": e.source, "f": e.file_path, "l": e.line}
         for e in found], default=str,
    )
    assert SECRET not in dumped, dumped


@pytest.mark.parametrize(
    "url",
    [
        "redis://user:12345;x@{h}:6379",        # ';' cuts the password into a fake port
        "redis://user:12345#x@{h}:6379",
        "redis://user:12345?x@{h}:6379",
        "jdbc:mysql://admin:123?x@{h}/db",
        "redis://user:12)3@{h}:6379",           # ')' ends the URL inside the password
        "redis://user:12]3@{h}:6379",
    ],
)
def test_a_password_that_cuts_the_url_never_becomes_a_host(url: str) -> None:
    src = f'U = "{url.format(h=CACHE)}"\n'
    found = edges("a.py", src)
    dumped = json.dumps([e.extra for e in found], default=str) + "".join(e.target for e in found)
    assert "user" not in dumped and "admin" not in dumped, dumped
    assert "12345" not in dumped and '"port": 12' not in dumped, dumped


def test_sample_directories_are_read_inside_the_repository_only() -> None:
    assert skip_path("/home/me/examples/svc/src/app.py", "/home/me/examples/svc") is False
    assert skip_path("/home/me/examples/svc/examples/app.py", "/home/me/examples/svc") is True
    assert skip_path("/home/me/svc/config/.env.example", "/home/me/svc") is True
    assert skip_path("/home/me/svc/src/main/resources/application.yml", "/home/me/svc") is False


def test_evidence_and_confidence_reach_the_awssdk_rows(tmp_path: Path) -> None:
    from code_review_graph.aws_sdk_links import read_edges
    from code_review_graph.graph import GraphStore

    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True)
    store = GraphStore(str(tmp_path / "app.db"))
    parser = CodeParser(repo)
    src = f'DB_HOST = "{RDS}"\ntargets = ["{CACHE}"]\n'
    (repo / "a.py").write_text(src)
    nodes, found = parser.parse_bytes(repo / "a.py", src.encode())
    store.store_file_nodes_edges(str(repo / "a.py"), nodes, found, "")
    try:
        rows = {r["name"]: r["extra"] for r in read_edges(store)}
    finally:
        store.close()
    assert rows["mydb"]["evidence"] == "config_key" and rows["mydb"]["confidence"] == 1.0
    assert rows["mycache"]["evidence"] == "hostname_only" and rows["mycache"]["confidence"] == 0.4

"""Edges from code to the databases, caches, search domains and load balancers it connects to.

``aws_sdk_calls`` records SDK operations on RDS, ElastiCache, OpenSearch/Elasticsearch and load
balancers (``rds.describe_db_instances``). This module records the other way code reaches the same
resources: a client connection that names a host. Both become the same edge, a ``REFERENCES`` edge
to ``external:aws-<service>:<name>`` with ``resolution == "aws_sdk"``, so the ``awssdk`` join
treats them alike. The service and kind come from the host:

========================================  =====================  =====================
host                                      service                kind
========================================  =====================  =====================
``<id>.<hash>.<region>.rds.amazonaws.com``  ``rds``              ``db-instance``
``<id>.cluster-<hash>.<region>.rds...``     ``rds``              ``db-cluster``
``<name>.proxy-<hash>.<region>.rds...``     ``rds``              ``db-proxy``
``<rg>.<hash>.ng.<n>.<r>.cache...``         ``elasticache``      ``replication-group``
``<cluster>.<hash>.<n>.<r>.cache...``       ``elasticache``      ``cache-cluster``
``search-<domain>-<id>.<r>.es...``          ``es``               ``search-domain``
``<name>-<id>.<r>.elb.amazonaws.com``       ``elasticloadbalancing``  ``load-balancer``
========================================  =====================  =====================

The name is the identifier the host spells: the first DNS label of an RDS or ElastiCache host,
the domain inside ``search-<domain>-<26 characters>``, the load balancer name before the numeric
or hexadecimal id. A host the rules cannot name (a custom DNS name, an ``internal-`` load
balancer, whose prefix may or may not be part of the name, a custom cluster endpoint, an
ElastiCache Serverless cache) is an edge with no name: ``unresolved`` and the host in ``host``.
Nothing is matched by a part of a name.

What counts as evidence of a connection:

* a ``jdbc:``/``r2dbc:`` URL (PostgreSQL, MySQL, MariaDB, SQL Server) or a ``redis://`` URL written
  in a string of code or in a Spring ``application*``/``bootstrap*`` file, or an AWS endpoint host
  written out;
* a client created from a library the file imports: psycopg2, psycopg, asyncpg, pymysql, MySQLdb,
  mysql.connector, aiomysql, pg8000, SQLAlchemy, redis-py, elasticsearch-py and opensearch-py in
  Python; Jedis, Lettuce, Spring Data Redis, Redisson, the Elasticsearch/OpenSearch REST clients
  (``HttpHost``, ``connectedTo``) and the driver data sources in Java; ``pg``, ``mysql``,
  ``mysql2``, ``ioredis``, ``redis``, ``@elastic/elasticsearch``, ``@opensearch-project/opensearch``
  and ``sequelize`` in JavaScript and TypeScript;
* the Spring keys ``spring.(data.)redis.host``, ``...cluster.nodes`` and the Elasticsearch ``uris``.

A host given as an environment variable or a property (``os.environ["DB_HOST"]``,
``${db.host}``) is an unresolved edge with its ``placeholder``; the value is never read.

Credentials never enter an edge. A URL is cut into the host, the port and the database name; the
user, the password and every query or property (``?password=``, ``;password=``) are dropped
before anything is stored, and a host that does not look like a host is dropped with them.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional

from .aws_sdk_calls import (
    _DYN,
    _MAX_LINE,
    _MAX_TEXT,
    Val,
    _Java,
    _Js,
    _line,
    _Out,
    _Python,
    _text,
    _walk,
)
from .config_keys import normalize_spring_config_key

__all__ = ["classify_host", "parse_targets", "scan_code", "scan_config", "scan_text", "wants"]

_MAX_TARGETS = 5
SQL, REDIS, SEARCH = "sql", "redis", "search"
_FAMILY_DEFAULT = {
    SQL: ("rds", "db-instance"),
    REDIS: ("elasticache", "cache-cluster"),
    SEARCH: ("es", "search-domain"),
}

# -- Hosts ---------------------------------------------------------------------------------------

_LOOPBACK = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal",
    "gateway.docker.internal", "kubernetes.docker.internal",
})
_DOC_HOSTS = frozenset({
    "host", "hostname", "your-host", "yourhost", "db-host", "dbhost", "redis-host", "server",
    "servername", "example", "xxx",
})
_DOC_SUFFIXES = (".example.com", ".example.org", ".example.net", ".invalid", ".test", ".localhost")
_HOST_OK = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,252}$")
_RDS = re.compile(r"^(?P<pre>.+?)\.rds\.amazonaws\.com(?:\.cn)?$")
_CACHE = re.compile(r"^(?P<pre>.+?)\.cache\.amazonaws\.com(?:\.cn)?$")
_ES = re.compile(r"^(?P<pre>.+?)\.(?:es\.amazonaws\.com(?:\.cn)?|aos\.[a-z0-9\-]+\.on\.aws)$")
_ELB = re.compile(r"^(?P<pre>.+?)\.elb(?:\.[a-z0-9\-]+)?\.amazonaws\.com(?:\.cn)?$")
_ELB_ID = re.compile(r"^(?P<n>.+)-(?:\d{6,}|[0-9a-f]{16})$")


class Classified(NamedTuple):
    service: str
    kind: str
    name: Optional[str]
    aws: bool
    reason: Optional[str] = None


def skip_host(host: str) -> bool:
    """A host that names this machine, a container network or a documentation example."""
    return (
        host in _LOOPBACK or host in _DOC_HOSTS or host.startswith("127.")
        or host.endswith(_DOC_SUFFIXES)
    )


def classify_host(host: str, family: str = SQL) -> Classified:
    """The service, kind and name an endpoint host spells, by the rules in the module docstring.

    A host of no AWS endpoint pattern keeps the service of its client *family* and has no name,
    so it can never equal a declared resource's name.
    """
    host = host.lower()
    found = _RDS.match(host)
    if found:
        pre = found.group("pre").split(".")
        if len(pre) < 3:
            return Classified("rds", "db-instance", None, True, "unrecognised_endpoint")
        second = pre[1]
        if second.startswith("cluster-custom-"):
            return Classified("rds", "db-cluster", None, True, "custom_endpoint")
        if second.startswith("cluster-"):
            return Classified("rds", "db-cluster", pre[0], True)
        if second.startswith("proxy-"):
            return Classified("rds", "db-proxy", pre[0], True)
        if second == "endpoint":
            return Classified("rds", "db-proxy", None, True, "proxy_endpoint")
        return Classified("rds", "db-instance", pre[0], True)
    found = _CACHE.match(host)
    if found:
        pre = found.group("pre").split(".")
        if len(pre) >= 2 and pre[0] in ("clustercfg", "master", "replica"):
            return Classified("elasticache", "replication-group", pre[1], True)
        if "serverless" in pre[1:3]:
            return Classified("elasticache", "cache-cluster", None, True, "serverless_cache")
        if len(pre) >= 3 and pre[2] == "ng":
            return Classified("elasticache", "replication-group", pre[0], True)
        return Classified("elasticache", "cache-cluster", pre[0], True)
    found = _ES.match(host)
    if found:
        first = found.group("pre").split(".")[0]
        head, _, tail = first.rpartition("-")
        if head.startswith(("search-", "vpc-")) and len(tail) == 26 and tail.isalnum():
            return Classified("es", "search-domain", head.split("-", 1)[1], True)
        return Classified("es", "search-domain", None, True, "unrecognised_endpoint")
    found = _ELB.match(host)
    if found:
        labels = found.group("pre").split(".")
        first = labels[1] if labels[0] == "dualstack" and len(labels) > 1 else labels[0]
        if first.startswith("internal-"):
            return Classified(
                "elasticloadbalancing", "load-balancer", None, True, "internal_prefix",
            )
        named = _ELB_ID.match(first)
        return Classified(
            "elasticloadbalancing", "load-balancer", named.group("n") if named else None, True,
            None if named else "unrecognised_endpoint",
        )
    service, kind = _FAMILY_DEFAULT.get(family, _FAMILY_DEFAULT[SQL])
    return Classified(service, kind, None, False, "non_aws_host")


# -- Connection strings --------------------------------------------------------------------------


class Target(NamedTuple):
    """One endpoint named by a connection string: no host when it is not written out."""

    host: Optional[str] = None
    placeholder: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    engine: Optional[str] = None


_URL = re.compile(
    r"^(?:(?:jdbc|r2dbc):)?(?:(?:pool|aws-wrapper):)*(?P<engine>[A-Za-z][A-Za-z0-9\-]*)"
    r"(?:\+[A-Za-z0-9_]+)?(?::[A-Za-z]+)?://(?P<rest>.*)$", re.S,
)
_HOST_PIECE = re.compile(
    r"^(?P<h>[A-Za-z0-9_.\-]+|\$\{[^}]*\}|\{[A-Za-z_][A-Za-z0-9_.]*\}|%s|\$[A-Za-z_]\w*)"
    r"(?::(?P<p>\d{1,5}))?$"
)
_PROP_HOST = re.compile(r"^\$\{([A-Za-z0-9_.\-]+)(?::[^}]*)?\}$")
_SIMPLE_DYN = re.compile(r"^(?:\{([A-Za-z_][A-Za-z0-9_.]*)\}|\$([A-Za-z_]\w*))$")
_DB_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_SQLSERVER_DB = re.compile(r";\s*database(?:name)?\s*=\s*([A-Za-z0-9_.\-]{1,128})", re.I)
_DSN_KEY = re.compile(r"(?:^|\s)(host|port|dbname)\s*=\s*(?:'([^']*)'|(\S+))")
_ENGINES = {
    "postgresql": "postgresql", "postgres": "postgresql", "mysql": "mysql", "mariadb": "mariadb",
    "sqlserver": "sqlserver", "mssql": "sqlserver", "redis": "redis", "rediss": "redis",
}


def _dyn_target(piece: str, port: Optional[int], database: Optional[str], engine: Optional[str],
                ) -> Target:
    placeholder: Optional[str] = None
    prop = _PROP_HOST.match(piece)
    simple = _SIMPLE_DYN.match(piece)
    if prop:
        key = prop.group(1)
        placeholder = normalize_spring_config_key(key) if "." in key else key
    elif simple:
        key = simple.group(1) or simple.group(2)
        placeholder = normalize_spring_config_key(key) if "." in key else key
    return Target(None, placeholder, port, database, engine)


def _pieces(authority: str, database: Optional[str], engine: Optional[str]) -> list[Target]:
    out: list[Target] = []
    for piece in authority.split(","):
        piece = piece.strip()
        if not piece:
            continue
        match = _HOST_PIECE.match(piece)
        port: Optional[int] = None
        if match is None:
            # Not a host any rule can read (a credential cut in the wrong place, IPv6, a
            # composite template): nothing of it is kept.
            out.append(Target(None, None, None, database, engine))
            continue
        if match.group("p") and 0 < int(match.group("p")) < 65536:
            port = int(match.group("p"))
        host = match.group("h")
        if _DYN in piece or host.startswith(("$", "{", "%")):
            out.append(_dyn_target(host, port, database, engine))
        elif _HOST_OK.match(host.lower()):
            out.append(Target(host.lower(), None, port, database, engine))
        else:
            out.append(Target(None, None, port, database, engine))
        if len(out) >= _MAX_TARGETS:
            break
    return out


def _parse_url(text: str, family: str) -> list[Target]:
    match = _URL.match(text)
    if match is None:
        return []
    engine = _ENGINES.get(match.group("engine").lower())
    rest = match.group("rest")
    # Everything after ``?``, ``#`` or ``;`` (query, fragment, driver properties) may hold a
    # credential and is not read, apart from the SQL Server database name.
    head = re.split(r"[?#;]", rest, maxsplit=1)[0]
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    authority, _, path = head.partition("/")
    database: Optional[str] = None
    if family == SQL:
        candidate = path.split("/", 1)[0]
        if candidate and _DB_NAME.match(candidate):
            database = candidate
        elif engine == "sqlserver":
            named = _SQLSERVER_DB.search(rest)
            database = named.group(1) if named else None
    if not authority:
        return [Target(None, None, None, database, engine)]
    return _pieces(authority, database, engine)


def _parse_dsn(text: str) -> list[Target]:
    """A libpq connection string; only ``host``, ``port`` and ``dbname`` are read."""
    found: dict[str, str] = {}
    for m in _DSN_KEY.finditer(text):
        found.setdefault(m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
    if "host" not in found:
        return []
    database = found.get("dbname")
    if database is not None and not _DB_NAME.match(database):
        database = None
    piece = found["host"] + (f":{found['port']}" if found.get("port", "").isdigit() else "")
    return _pieces(piece, database, "postgresql")


def parse_targets(text: str, family: str) -> list[Target]:
    """The endpoints a URL, a ``host[:port][,host[:port]]`` list or a libpq string names."""
    text = text.strip()
    if "://" in text:
        return _parse_url(text, family)
    if family == SQL and "=" in text and " " in text and "host" in text:
        return _parse_dsn(text)
    if re.search(r"[\s=]", text):
        return []
    return _pieces(text, None, None)


# -- Emitting ------------------------------------------------------------------------------------


def _emit(
    out: _Out, targets: list[Target], family: str, line: int, sdk: str, client: str,
    engine: Optional[str] = None,
) -> None:
    for target in targets[:_MAX_TARGETS]:
        extra: dict[str, Any] = {"client": client}
        eng = target.engine or engine
        if eng:
            extra["engine"] = eng
        if target.port:
            extra["port"] = target.port
        if target.database:
            extra["database"] = target.database
        if target.host is not None:
            if skip_host(target.host):
                continue
            found = classify_host(target.host, family)
            extra["host"] = target.host[:253]
            extra["aws_endpoint"] = found.aws
            if found.name is None and found.reason:
                extra["unresolved_reason"] = found.reason
            service, kind, value = found.service, found.kind, Val(found.name)
        else:
            service, kind = _FAMILY_DEFAULT[family]
            value = Val(None, target.placeholder)
        out.add(service, "connect", value, line, sdk, kind=kind, access="connect", extra=extra)


def _emit_val(
    out: _Out, val: Val, family: str, line: int, sdk: str, client: str,
    engine: Optional[str] = None, port: Optional[int] = None,
    database: Optional[str] = None,
) -> None:
    """Emit what a value says: the targets of its text, or the placeholder it stands for."""
    if val.template is None:
        targets = [Target(None, val.placeholder, port, database, engine)]
    else:
        targets = parse_targets(val.template, family)
        if not targets:
            return
        targets = [
            t._replace(port=t.port or port, database=t.database or database,
                       engine=t.engine or engine)
            for t in targets
        ]
    _emit(out, targets, family, line, sdk, client, engine)


# -- Text: URLs and endpoint hosts in strings and Spring files -----------------------------------

_URL_IN_TEXT = re.compile(
    r"(?:(?:jdbc|r2dbc):(?:pool:|aws-wrapper:)*(?:postgresql|postgres|mysql|mariadb|sqlserver)"
    r"(?::[A-Za-z]+)?://|rediss?://)[^\s\"'`<>)\]]*", re.I,
)
_HOST_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9._\-])((?:[A-Za-z0-9][A-Za-z0-9\-]*\.){1,8}"
    r"(?:rds\.amazonaws\.com|cache\.amazonaws\.com|es\.amazonaws\.com|elb\.amazonaws\.com"
    r"|elb\.[a-z0-9\-]+\.amazonaws\.com|aos\.[a-z0-9\-]+\.on\.aws)(?:\.cn)?)(?![A-Za-z0-9\-])",
    re.I,
)
_COMMENT_START = ("#", "//", "*", "/*", "--", "!")
_CONFIG_FILE = re.compile(
    r"^(?:application|bootstrap)(?:-[A-Za-z0-9_.\-]+)?\.(?:ya?ml|properties)$"
)
_REDIS_HOST_KEYS = frozenset({"spring.redis.host", "spring.data.redis.host"})
_REDIS_NODE_KEYS = frozenset({
    "spring.redis.cluster.nodes", "spring.data.redis.cluster.nodes",
})
_SEARCH_KEYS = frozenset({
    "spring.elasticsearch.uris", "spring.elasticsearch.rest.uris", "spring.opensearch.uris",
    "spring.data.elasticsearch.client.reactive.endpoints",
})


def _family_of_url(text: str) -> str:
    return REDIS if text.lower().startswith("redis") else SQL


def scan_text(text: str, out: _Out, sdk: str = "jdbc-url") -> None:
    """URLs and AWS endpoint hosts written in the lines of *text* (a source or config file)."""
    for number, line_text in enumerate(text[:_MAX_TEXT].splitlines(), 1):
        line_text = line_text[:_MAX_LINE]
        stripped = line_text.lstrip()
        if stripped.startswith(_COMMENT_START) or (
            "jdbc:" not in line_text.lower() and "r2dbc:" not in line_text.lower()
            and "redis" not in line_text.lower() and "amazonaws.com" not in line_text
            and ".on.aws" not in line_text
        ):
            continue
        for m in _URL_IN_TEXT.finditer(line_text):
            url = m.group(0)
            family = _family_of_url(url)
            client = "jdbc" if url.lower().startswith(("jdbc", "r2dbc")) else "redis-url"
            _emit(out, parse_targets(url, family), family, number,
                  sdk if client == "jdbc" else "endpoint-literal", client)
        for m in _HOST_IN_TEXT.finditer(line_text):
            host = m.group(1).lower()
            if skip_host(host):
                continue
            _emit(out, [Target(host)], SQL, number, "endpoint-literal", "endpoint-host")


def _yaml_entries(lines: list[str]) -> list[tuple[int, str, str]]:
    """``(line, dotted key, scalar value)`` of the plain scalars of a simple YAML file.

    Nested mappings and ``- item`` lists under a key are followed by indentation; flow
    collections, anchors and multi-line scalars are not read.
    """
    out: list[tuple[int, str, str]] = []
    stack: list[tuple[int, str]] = []
    for number, raw in enumerate(lines, 1):
        raw = raw[:_MAX_LINE]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "---":
            stack.clear()
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if stripped.startswith("- "):
            item = re.sub(r"\s+#.*$", "", stripped[2:].strip()).strip("'\"")
            while stack and stack[-1][0] > indent:
                stack.pop()
            if stack and item and not re.match(r"^[\w\-.]+:\s", item):
                out.append((number, ".".join(k for _, k in stack), item))
            continue
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*:(?:\s+(.*))?$", stripped)
        if m is None:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        value = re.sub(r"\s+#.*$", "", (m.group(2) or "").strip()).strip("'\"")
        if not value:
            stack.append((indent, m.group(1)))
        else:
            out.append((number, ".".join([k for _, k in stack] + [m.group(1)]), value))
    return out


def _properties_entries(lines: list[str]) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for number, raw in enumerate(lines, 1):
        raw = raw[:_MAX_LINE].strip()
        if not raw or raw.startswith(("#", "!")):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*[=:]\s*(.*)$", raw)
        if m:
            out.append((number, m.group(1), m.group(2).strip().strip("'\"")))
    return out


def scan_config(name: str, text: str, out: _Out) -> None:
    """A Spring ``application*``/``bootstrap*`` file: its URLs, endpoint hosts and host keys."""
    if not _CONFIG_FILE.match(name):
        return
    scan_text(text, out, sdk="spring-config")
    lines = text[:_MAX_TEXT].splitlines()
    entries = (
        _properties_entries(lines) if name.endswith(".properties") else _yaml_entries(lines)
    )
    for number, key, value in entries:
        norm = normalize_spring_config_key(re.sub(r"\[[0-9]+\]$", "", key))
        if norm in _REDIS_HOST_KEYS:
            _emit_val(out, Val(value), REDIS, number, "spring-config", "spring-redis")
        elif norm in _REDIS_NODE_KEYS:
            _emit_val(out, Val(value), REDIS, number, "spring-config", "spring-redis")
        elif norm in _SEARCH_KEYS:
            _emit_val(out, Val(value), SEARCH, number, "spring-config", "spring-elasticsearch")


# -- Python clients ------------------------------------------------------------------------------


class _Client(NamedTuple):
    family: str
    engine: Optional[str]
    #: What the first positional argument is: ``host``, ``url`` (a URL, DSN or host list) or ``""``.
    pos0: str
    host_kws: tuple[str, ...] = ("host",)
    url_kws: tuple[str, ...] = ("dsn", "conninfo", "url", "connection_string")


def _py_clients() -> dict[str, _Client]:
    table: dict[str, _Client] = {}
    for origin in ("psycopg2.connect", "psycopg.connect", "psycopg_pool.ConnectionPool",
                   "psycopg_pool.AsyncConnectionPool", "asyncpg.connect",
                   "asyncpg.create_pool", "pg8000.connect", "pg8000.dbapi.connect"):
        table[origin] = _Client(SQL, "postgresql", "url")
    for origin in ("pymysql.connect", "MySQLdb.connect", "aiomysql.connect",
                   "aiomysql.create_pool", "mysql.connector.connect",
                   "mysql.connector.pooling.MySQLConnectionPool"):
        table[origin] = _Client(SQL, "mysql", "host")
    for origin in ("sqlalchemy.create_engine", "sqlalchemy.engine.create_engine",
                   "sqlalchemy.ext.asyncio.create_async_engine", "sqlmodel.create_engine"):
        table[origin] = _Client(SQL, None, "url", ("host",), ("url",))
    for origin in ("sqlalchemy.engine.URL.create", "sqlalchemy.URL.create",
                   "sqlalchemy.engine.url.URL.create"):
        table[origin] = _Client(SQL, None, "", ("host",), ())
    for base in ("redis", "redis.asyncio", "aioredis"):
        for cls in ("Redis", "StrictRedis"):
            table[f"{base}.{cls}"] = _Client(REDIS, "redis", "host", ("host",), ("url",))
            table[f"{base}.{cls}.from_url"] = _Client(REDIS, "redis", "url", (), ("url",))
        table[f"{base}.from_url"] = _Client(REDIS, "redis", "url", (), ("url",))
        table[f"{base}.ConnectionPool"] = _Client(REDIS, "redis", "host", ("host",), ("url",))
        table[f"{base}.ConnectionPool.from_url"] = _Client(REDIS, "redis", "url", (), ("url",))
    for origin in ("redis.cluster.RedisCluster", "redis.RedisCluster",
                   "redis.asyncio.cluster.RedisCluster", "redis.asyncio.RedisCluster",
                   "rediscluster.RedisCluster"):
        table[origin] = _Client(REDIS, "redis", "host", ("host",), ("url",))
    for origin in ("elasticsearch.Elasticsearch", "elasticsearch.AsyncElasticsearch",
                   "opensearchpy.OpenSearch", "opensearchpy.AsyncOpenSearch"):
        table[origin] = _Client(SEARCH, None, "url", ("hosts", "host"), ("hosts",))
    return table


_PY_CLIENTS = _py_clients()
_PY_DB_KWS = ("database", "dbname", "db")


class _PyStores:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.py = _Python(src, root, out)
        self.imports: dict[str, str] = {}
        for node in _walk(root):
            if node.type == "import_statement":
                for c in node.named_children:
                    if c.type == "dotted_name":
                        local = _text(c, src).split(".")[0]
                        self.imports[local] = local
                    elif c.type == "aliased_import":
                        n, a = c.child_by_field_name("name"), c.child_by_field_name("alias")
                        if n is not None and a is not None:
                            self.imports[_text(a, src)] = _text(n, src)
            elif node.type == "import_from_statement":
                mod = node.child_by_field_name("module_name")
                if mod is None or mod.type != "dotted_name":
                    continue
                base = _text(mod, src)
                for c in node.children_by_field_name("name"):
                    if c.type == "dotted_name":
                        self.imports[_text(c, src)] = f"{base}.{_text(c, src)}"
                    elif c.type == "aliased_import":
                        n, a = c.child_by_field_name("name"), c.child_by_field_name("alias")
                        if n is not None and a is not None:
                            self.imports[_text(a, src)] = f"{base}.{_text(n, src)}"

    def run(self) -> None:
        for node in _walk(self.root):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None or fn.type not in ("identifier", "attribute"):
                continue
            head, _, rest = _text(fn, self.src).partition(".")
            origin = self.imports.get(head)
            if origin is None:
                continue
            spec = _PY_CLIENTS.get(origin + ("." + rest if rest else ""))
            if spec is not None:
                self._site(node, spec, origin + ("." + rest if rest else ""))

    def _values(self, node: Any) -> list[Val]:
        """The strings an argument holds: a string, or the items of a list or tuple of them."""
        if node.type in ("list", "tuple", "set"):
            out: list[Val] = []
            for item in node.named_children[:_MAX_TARGETS]:
                if item.type == "dictionary":
                    out.extend(self._dict_hosts(item))
                else:
                    out.append(self.py.val(item))
            return out
        if node.type == "dictionary":
            return self._dict_hosts(node)
        return [self.py.val(node)]

    def _dict_hosts(self, node: Any) -> list[Val]:
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
            if key is not None and value is not None \
                    and self.py.val(key).template == "host":
                return [self.py.val(value)]
        return []

    def _site(self, call: Any, spec: _Client, origin: str) -> None:
        pos, kws, _splat = self.py._args(call)
        line = _line(call)
        nodes: list[Any] = [kws[k] for k in (*spec.host_kws, *spec.url_kws) if k in kws][:1]
        if not nodes and pos and spec.pos0:
            nodes = [pos[0]]
        if not nodes:
            return
        database = None
        for k in _PY_DB_KWS:
            if k in kws:
                database = self.py.val(kws[k]).template
                if database is not None and not _DB_NAME.match(database):
                    database = None
                break
        port: Optional[int] = None
        if "port" in kws and kws["port"].type == "integer":
            digits = _text(kws["port"], self.src)
            port = int(digits) if digits.isdigit() and 0 < int(digits) < 65536 else None
        client = origin.split(".")[0]
        for val in self._values(nodes[0]):
            _emit_val(self.out, val, spec.family, line, "client-call", client[:64],
                      spec.engine, port, database)


# -- Java clients --------------------------------------------------------------------------------


_JEDIS_CLASSES = frozenset({"Jedis", "JedisPool", "JedisPooled", "HostAndPort", "JedisShardInfo"})
_SPRING_REDIS_CLASSES = frozenset({
    "RedisStandaloneConfiguration", "LettuceConnectionFactory", "JedisConnectionFactory",
})
_LETTUCE_FACTORIES = frozenset({"RedisURI", "RedisClient", "RedisClusterClient"})
_ES_IMPORTS = (
    "org.elasticsearch.client", "org.opensearch.client", "co.elastic.clients",
    "org.springframework.data.elasticsearch",
)
_DRIVER_IMPORTS = {
    "org.postgresql": "postgresql", "com.mysql": "mysql", "org.mariadb": "mariadb",
    "com.microsoft.sqlserver": "sqlserver",
}


class _JavaStores:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.java = _Java(src, root, out)
        self.imports: list[str] = []
        for node in _walk(root):
            if node.type == "import_declaration":
                text = _text(node, src).replace("import", "", 1).replace("static", "", 1)
                self.imports.append(text.strip().rstrip(";").strip())

    def _has(self, *prefixes: str) -> bool:
        return any(i.startswith(prefixes) for i in self.imports)

    def _driver(self) -> Optional[str]:
        for prefix, engine in _DRIVER_IMPORTS.items():
            if self._has(prefix):
                return engine
        return None

    def run(self) -> None:
        jedis = self._has("redis.clients.jedis")
        lettuce = self._has("io.lettuce.core")
        spring_redis = self._has("org.springframework.data.redis")
        redisson = self._has("org.redisson")
        search = self._has(*_ES_IMPORTS)
        driver = self._driver()
        if not (jedis or lettuce or spring_redis or redisson or search or driver):
            return
        for node in _walk(self.root):
            if node.type == "object_creation_expression":
                t = node.child_by_field_name("type")
                name = _text(t, self.src).split("<", 1)[0] if t is not None else ""
                args = self._args(node)
                if name in _JEDIS_CLASSES and jedis:
                    self._first_host(node, args, REDIS, "jedis")
                elif name in _SPRING_REDIS_CLASSES and spring_redis:
                    self._first_host(node, args, REDIS, "spring-data-redis")
                elif name == "HttpHost" and search:
                    self._first_host(node, args, SEARCH, "HttpHost", skip=("http", "https"))
            elif node.type == "method_invocation":
                self._method(node, jedis, lettuce, spring_redis, redisson, search, driver)

    def _args(self, node: Any) -> list[Any]:
        args = node.child_by_field_name("arguments")
        return list(args.named_children) if args is not None else []

    def _first_host(
        self, node: Any, args: list[Any], family: str, client: str,
        skip: tuple[str, ...] = (), engine: Optional[str] = None, known_only: bool = False,
    ) -> None:
        """Emit the first argument that is a host: a string, or a property or variable."""
        if not args:
            return
        chosen: Optional[Val] = None
        for arg in args:
            val = self.java.val(arg)
            if val.template is not None and val.template in skip:
                continue
            if val.template is not None or val.placeholder is not None:
                chosen = val
                break
        if chosen is None:
            if known_only:
                return
            chosen = Val()
        _emit_val(self.out, chosen, family, _line(node), "client-call", client, engine)

    def _method(
        self, node: Any, jedis: bool, lettuce: bool, spring_redis: bool, redisson: bool,
        search: bool, driver: Optional[str],
    ) -> None:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None:
            return
        name = _text(name_node, self.src)
        recv = _text(obj, self.src) if obj is not None else ""
        args = self._args(node)
        if lettuce and name in ("create", "redis", "rediss", "withHost") and (
            recv.split(".")[0].split("(")[0] in _LETTUCE_FACTORIES or "RedisURI" in recv
        ):
            self._first_host(
                node, args, REDIS, "lettuce",
                known_only=recv in ("RedisClient", "RedisClusterClient"),
            )
        elif spring_redis and name == "setHostName":
            self._first_host(node, args, REDIS, "spring-data-redis")
        elif redisson and name in ("setAddress", "addNodeAddress", "addNodeAddresses"):
            self._first_host(node, args, REDIS, "redisson")
        elif search and name == "create" and recv == "HttpHost":
            self._first_host(node, args, SEARCH, "HttpHost")
        elif search and name == "connectedTo":
            for arg in args[:_MAX_TARGETS]:
                _emit_val(self.out, self.java.val(arg), SEARCH, _line(node), "client-call",
                          "spring-data-elasticsearch")
        elif driver and name in ("setServerName", "setServerNames"):
            self._first_host(node, args, SQL, "datasource", engine=driver)


# -- JavaScript / TypeScript clients -------------------------------------------------------------

#: ``(package, export)`` -> ``(family, engine)``; ``default`` is the package's default export.
_JS_CLIENTS: dict[tuple[str, str], tuple[str, Optional[str]]] = {
    ("pg", "Pool"): (SQL, "postgresql"), ("pg", "Client"): (SQL, "postgresql"),
    ("mysql", "createConnection"): (SQL, "mysql"), ("mysql", "createPool"): (SQL, "mysql"),
    ("mysql2", "createConnection"): (SQL, "mysql"), ("mysql2", "createPool"): (SQL, "mysql"),
    ("mysql2/promise", "createConnection"): (SQL, "mysql"),
    ("mysql2/promise", "createPool"): (SQL, "mysql"),
    ("sequelize", "Sequelize"): (SQL, None),
    ("ioredis", "default"): (REDIS, "redis"), ("ioredis", "Redis"): (REDIS, "redis"),
    ("ioredis", "Cluster"): (REDIS, "redis"), ("redis", "createClient"): (REDIS, "redis"),
    ("redis", "createCluster"): (REDIS, "redis"),
    ("@elastic/elasticsearch", "Client"): (SEARCH, None),
    ("@opensearch-project/opensearch", "Client"): (SEARCH, None),
}
_JS_PACKAGES = frozenset(pkg for pkg, _ in _JS_CLIENTS)
_JS_HOST_KEYS = ("host", "hostname")
_JS_URL_KEYS = ("connectionstring", "uri", "url", "node", "nodes", "hosts")


class _JsStores:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.js = _Js(src, root, out)
        #: local name -> (package, export or ``None`` for a default or namespace import)
        self.locals: dict[str, tuple[str, Optional[str]]] = {}
        for node in _walk(root):
            if node.type == "import_statement":
                self._import(node)
            elif node.type == "variable_declarator":
                self._require(node)

    def _import(self, node: Any) -> None:
        pkg = self.js._string(node.child_by_field_name("source"))
        if pkg not in _JS_PACKAGES:
            return
        for spec in _walk(node):
            if spec.type == "import_specifier":
                n, a = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                if n is not None:
                    self.locals[_text(a or n, self.src)] = (pkg, _text(n, self.src))
            elif spec.type == "namespace_import" and spec.named_child_count:
                self.locals[_text(spec.named_children[0], self.src)] = (pkg, None)
            elif spec.type == "import_clause":
                for c in spec.children:
                    if c.type == "identifier":
                        self.locals[_text(c, self.src)] = (pkg, None)

    def _require(self, node: Any) -> None:
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is None or value is None:
            return
        if value.type == "await_expression" and value.named_child_count:
            value = value.named_children[0]
        if value.type != "call_expression":
            return
        fn, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
        if fn is None or _text(fn, self.src) != "require" or args is None \
                or not args.named_child_count:
            return
        pkg = self.js._string(args.named_children[0])
        if pkg not in _JS_PACKAGES:
            return
        if name.type == "identifier":
            self.locals[_text(name, self.src)] = (pkg, None)
        elif name.type == "object_pattern":
            for c in name.named_children:
                if c.type == "shorthand_property_identifier_pattern":
                    self.locals[_text(c, self.src)] = (pkg, _text(c, self.src))
                elif c.type == "pair_pattern":
                    k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                    if k is not None and v is not None:
                        self.locals[_text(v, self.src)] = (pkg, _text(k, self.src))

    def _resolve(self, fn: Any) -> Optional[tuple[str, str]]:
        if fn.type == "identifier":
            found = self.locals.get(_text(fn, self.src))
            return (found[0], found[1] or "default") if found else None
        if fn.type == "member_expression":
            obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
            if obj is not None and prop is not None and obj.type == "identifier":
                found = self.locals.get(_text(obj, self.src))
                if found and found[1] is None:
                    return found[0], _text(prop, self.src)
        return None

    def run(self) -> None:
        if not self.locals:
            return
        for node in _walk(self.root):
            if node.type == "new_expression":
                fn = node.child_by_field_name("constructor")
            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
            else:
                continue
            key = self._resolve(fn) if fn is not None else None
            spec = _JS_CLIENTS.get(key) if key else None
            if spec is not None and key is not None:
                self._site(node, spec, key)

    def _pairs(self, obj: Any) -> dict[str, Any]:
        pairs: dict[str, Any] = {}
        for c in obj.named_children:
            if c.type == "pair":
                k = c.child_by_field_name("key")
                if k is not None:
                    key = self.js._string(k) if k.type == "string" else _text(k, self.src)
                    pairs[(key or "").lower()] = c.child_by_field_name("value")
            elif c.type == "shorthand_property_identifier":
                pairs[_text(c, self.src).lower()] = c
        return pairs

    def _val(self, node: Any) -> Val:
        if node.type == "shorthand_property_identifier":
            const = self.js.consts.get(_text(node, self.src))
            return Val(const) if const is not None else Val()
        return self.js.val(node)

    def _node_vals(self, node: Any) -> list[Val]:
        if node.type == "array":
            out: list[Val] = []
            for item in node.named_children[:_MAX_TARGETS]:
                if item.type == "object":
                    pairs = self._pairs(item)
                    chosen = next((pairs[k] for k in (*_JS_HOST_KEYS, "url", "node") if k in pairs),
                                  None)
                    if chosen is not None:
                        out.append(self._val(chosen))
                else:
                    out.append(self._val(item))
            return out
        return [self._val(node)]

    def _site(self, call: Any, spec: tuple[str, Optional[str]], key: tuple[str, str]) -> None:
        family, engine = spec
        args = call.child_by_field_name("arguments")
        named = list(args.named_children) if args is not None else []
        if not named:
            return
        first = named[0]
        line = _line(call)
        vals: list[Val] = []
        database: Optional[str] = None
        port: Optional[int] = None
        if first.type == "object":
            pairs = self._pairs(first)
            node = next((pairs[k] for k in (*_JS_HOST_KEYS, *_JS_URL_KEYS) if k in pairs), None)
            if node is None and "socket" in pairs and pairs["socket"].type == "object":
                inner = self._pairs(pairs["socket"])
                node = next((inner[k] for k in _JS_HOST_KEYS if k in inner), None)
            if node is not None:
                vals = self._node_vals(node)
            if "rootnodes" in pairs:
                vals = self._node_vals(pairs["rootnodes"])
            if "database" in pairs:
                database = self._val(pairs["database"]).template
        elif first.type == "array":
            vals = self._node_vals(first)
        elif key[0] == "sequelize":
            options = named[3] if len(named) >= 4 and named[3].type == "object" else None
            lead = self._val(first)
            if lead.template is not None and "://" in lead.template:
                vals = [lead]
            elif options is not None:
                pairs = self._pairs(options)
                if "host" in pairs:
                    vals = [self._val(pairs["host"])]
                database = lead.template
        elif first.type == "number":
            # ``new Redis(6379, "host")``
            if len(named) > 1:
                vals = [self._val(named[1])]
        else:
            lead = self._val(first)
            if lead.template is not None or lead.placeholder is not None:
                vals = [lead]
        if database is not None and not _DB_NAME.match(database):
            database = None
        for val in vals:
            _emit_val(self.out, val, family, line, "client-call", key[0][:64], engine, port,
                      database)


# -- Entry points --------------------------------------------------------------------------------

_AST_MARKERS: dict[str, tuple[bytes, ...]] = {
    "python": (
        b"psycopg", b"pymysql", b"MySQLdb", b"mysql", b"asyncpg", b"aiomysql", b"pg8000",
        b"sqlalchemy", b"sqlmodel", b"redis", b"elasticsearch", b"opensearch",
    ),
    "java": (
        b"redis.clients", b"io.lettuce", b"org.springframework.data.redis", b"org.redisson",
        b"elasticsearch", b"opensearch", b"org.postgresql", b"com.mysql", b"org.mariadb",
        b"com.microsoft.sqlserver", b"co.elastic",
    ),
    "javascript": (b"'pg'", b'"pg"', b"mysql", b"ioredis", b"redis", b"@elastic/",
                   b"@opensearch-project", b"sequelize"),
}
_AST_MARKERS["typescript"] = _AST_MARKERS["tsx"] = _AST_MARKERS["javascript"]
_TEXT_MARKERS = (b"jdbc:", b"r2dbc:", b"redis://", b"rediss://", b".amazonaws.com", b".on.aws")
_TEXT_SUFFIXES = frozenset({
    ".py", ".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".scala",
    ".sc", ".kt", ".kts", ".groovy",
})
_CONFIG_SUFFIXES = frozenset({".yml", ".yaml", ".properties"})


def wants(
    suffix: str, name: str, language: Optional[str], source: bytes,
) -> tuple[bool, bool, bool]:
    """``(read the syntax tree, scan the text, read as a Spring config file)`` for a file."""
    ast_ok = bool(language) and any(m in source for m in _AST_MARKERS.get(language or "", ()))
    text_ok = suffix in _TEXT_SUFFIXES and any(m in source for m in _TEXT_MARKERS)
    config_ok = suffix in _CONFIG_SUFFIXES and _CONFIG_FILE.match(name) is not None
    return ast_ok, text_ok, config_ok


def scan_code(language: str, src: bytes, root: Any, out: _Out) -> None:
    """The client connections of a parsed Python, Java, JavaScript or TypeScript file."""
    runner: Any = {"python": _PyStores, "java": _JavaStores}.get(language, _JsStores)
    runner(src, root, out).run()

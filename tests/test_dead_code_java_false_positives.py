"""Dead code on Java: constructors, Spring-wired classes, static imports, typed fields.

Every fixture is synthetic. Each test builds a small multi-file project, parses it
with the real parser, stores it and asks ``find_dead_code`` what is unused.
"""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code


def _build(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    paths = []
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    (repo / ".git").mkdir(exist_ok=True)
    parser = CodeParser(repo)
    store = GraphStore(tmp_path / "graph.db")
    parsed = {}
    for path in paths:
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
        parsed[path.name] = (nodes, edges)
    return repo, store, parsed


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
    """``Class.member`` (or ``Class`` for a class) of every reported symbol."""
    repo, store, _ = _build(tmp_path, files)
    try:
        return {
            d["qualified_name"].split("::")[-1] for d in find_dead_code(store, root=repo)
        }
    finally:
        store.close()


SRC = "src/main/java/shop/"

# --- 1 + 2: constructors, utility classes ---------------------------------------

KEYS = """package shop;
public final class Keys {
  public static final String PREFIX = "k-";
  private Keys() {}
  public static String key(String id) { return PREFIX + id; }
}
"""
LOMBOK_UTIL = """package shop;
import lombok.AccessLevel;
import lombok.NoArgsConstructor;
@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class Clocks {
  public static long now() { return 1L; }
}
"""
FORGOTTEN_UTIL = """package shop;
public final class Forgotten {
  private Forgotten() {}
  public static String nothing() { return ""; }
}
"""
SHAPES = """package shop;
public enum Shape {
  SQUARE(4), TRIANGLE(3);
  private final int sides;
  Shape(int sides) { this.sides = sides; }
  public int sides() { return sides; }
}
"""
OUTER = """package shop;
public class Outer {
  private static class Inner {
    private final int n;
    Inner(int n) { this.n = n; }
    int n() { return n; }
  }
  public int run() { return new Inner(2).n(); }
}
"""
MONEY = """package shop;
public record Money(long cents, String currency) {
  public Money {
    if (cents < 0) throw new IllegalArgumentException();
  }
  public Money(long cents) { this(cents, "USD"); }
  public long cents() { return cents; }
}
"""
UTIL_USER = """package shop.app;
import shop.Keys;
import shop.Clocks;
import shop.Shape;
import shop.Outer;
import shop.Money;
public class UtilUser {
  public String go() {
    return Keys.key("a") + Clocks.now() + Shape.SQUARE.sides()
        + new Outer().run() + new Money(1L);
  }
}
"""

CONSTRUCTOR_FILES = {
    SRC + "Keys.java": KEYS,
    SRC + "Clocks.java": LOMBOK_UTIL,
    SRC + "Forgotten.java": FORGOTTEN_UTIL,
    SRC + "Shape.java": SHAPES,
    SRC + "Outer.java": OUTER,
    SRC + "Money.java": MONEY,
    SRC + "app/UtilUser.java": UTIL_USER,
}


def test_constructors_are_never_reported(tmp_path):
    dead = _dead(tmp_path, CONSTRUCTOR_FILES)
    constructors = {
        d for d in dead
        if len(d.split(".")) > 1 and d.split(".")[-1] == d.split(".")[-2]
    }
    assert constructors == set()


def test_utility_class_is_judged_by_its_static_members(tmp_path):
    dead = _dead(tmp_path, CONSTRUCTOR_FILES)
    # Used through ``Keys.key(...)``, ``Clocks.now()`` from another package.
    assert "Keys" not in dead
    assert "Clocks" not in dead
    # The private constructor must not make an unused utility class look used or dead
    # for its constructor: the class is reported, the constructor is not.
    assert "Forgotten" in dead
    assert "Forgotten.Forgotten" not in dead
    assert "Forgotten.nothing" in dead


def test_enum_record_and_inner_class_are_judged_as_types(tmp_path):
    dead = _dead(tmp_path, CONSTRUCTOR_FILES)
    assert not {"Shape", "Outer", "Money", "Outer.Inner"} & dead


def test_record_is_a_class_with_components_and_constructors(tmp_path):
    _, store, parsed = _build(tmp_path, {SRC + "Money.java": MONEY})
    try:
        nodes, _ = parsed["Money.java"]
        record = next(n for n in nodes if n.kind == "Class" and n.name == "Money")
        assert record.extra["type_form"] == "record"
        assert record.extra["record_components"] == ["cents", "currency"]
        constructors = [n for n in nodes if n.extra.get("java_constructor")]
        # compact canonical constructor + the explicit one
        assert len(constructors) == 2
        assert {n.parent_name for n in constructors} == {"Money"}
    finally:
        store.close()


# --- 3: Spring-wired classes -----------------------------------------------------

WIRED = {
    SRC + "OrderController.java": """package shop;
import org.springframework.web.bind.annotation.RestController;
@RestController
public class OrderController {
  public String list() { return ""; }
}
""",
    SRC + "Mailer.java": """package shop;
import org.springframework.stereotype.Component;
@Component
public class Mailer {
  public Mailer() {}
  public void setRetries(int retries) {}
  public void send() {}
}
""",
    SRC + "AppConfig.java": """package shop;
import org.springframework.context.annotation.Configuration;
@Configuration
public class AppConfig {}
""",
    SRC + "ShopProperties.java": """package shop;
import org.springframework.boot.context.properties.ConfigurationProperties;
@ConfigurationProperties(prefix = "shop")
public class ShopProperties {
  private String region;
  private java.util.Map<String, Integer> limits;
  public void setRegion(String region) { this.region = region; }
  public void setLimits(java.util.Map<String, Integer> limits) { this.limits = limits; }
  public String getRegion() { return region; }
  public void forgottenHelper() {}
}
""",
    SRC + "RateProperties.java": """package shop;
import org.springframework.boot.context.properties.ConfigurationProperties;
@ConfigurationProperties(prefix = "rate")
public record RateProperties(int perMinute, String unit) {
  public int perMinute() { return perMinute; }
  public void neverCalled() {}
}
""",
    SRC + "PlainBean.java": """package shop;
public class PlainBean {
  public void setThing(String thing) {}
}
""",
}


def test_spring_wired_classes_and_bound_members_are_not_dead(tmp_path):
    dead = _dead(tmp_path, WIRED)
    for name in ("OrderController", "Mailer", "AppConfig", "ShopProperties", "RateProperties"):
        assert name not in dead, name
    # Constructors of wired classes.
    assert "Mailer.Mailer" not in dead
    # Setters bound by property name, and a record's own accessor.
    assert "ShopProperties.setRegion" not in dead
    assert "ShopProperties.setLimits" not in dead
    assert "Mailer.setRetries" not in dead
    assert "RateProperties.perMinute" not in dead


def test_wiring_does_not_hide_ordinary_members(tmp_path):
    dead = _dead(tmp_path, WIRED)
    # Only the framework-reached members are exempt; other members still need a caller.
    assert "ShopProperties.forgottenHelper" in dead
    assert "RateProperties.neverCalled" in dead
    assert "Mailer.send" in dead
    assert "OrderController.list" in dead
    # A class Spring does not manage keeps every member candidate, setters included.
    assert "PlainBean" in dead
    assert "PlainBean.setThing" in dead


# --- 4: static imports -----------------------------------------------------------

STATIC_FILES = {
    SRC + "util/Slugs.java": """package shop.util;
public final class Slugs {
  public static final String SEP = "-";
  private Slugs() {}
  public static String slugify(String s) { return s + SEP; }
  public static String unusedStatic() { return ""; }
}
""",
    SRC + "util/Limits.java": """package shop.util;
public final class Limits {
  public static final int MAX = 10;
  private Limits() {}
}
""",
    SRC + "util/Everything.java": """package shop.util;
public final class Everything {
  private Everything() {}
  public static int answer() { return 42; }
}
""",
    SRC + "util/Orphan.java": """package shop.util;
public final class Orphan {
  public static final int NOBODY = 1;
  private Orphan() {}
}
""",
    SRC + "app/Pages.java": """package shop.app;
import static shop.util.Slugs.slugify;
import static shop.util.Limits.MAX;
import static shop.util.Everything.*;
import static java.util.Collections.emptyList;
public class Pages {
  public String url(String title) {
    emptyList();
    return slugify(title) + MAX + answer();
  }
}
""",
}


def test_static_imports_resolve_to_the_class_and_member(tmp_path):
    repo, store, parsed = _build(tmp_path, STATIC_FILES)
    try:
        _, edges = parsed["Pages.java"]
        slugs = str(repo / SRC / "util/Slugs.java")
        limits = str(repo / SRC / "util/Limits.java")
        everything = str(repo / SRC / "util/Everything.java")
        refs = {e.target for e in edges if e.kind == "REFERENCES"}
        assert f"{slugs}::Slugs" in refs
        assert f"{limits}::Limits" in refs
        assert f"{everything}::Everything" in refs
        calls = {e.target for e in edges if e.kind == "CALLS"}
        assert f"{slugs}::Slugs.slugify" in calls
        # A JDK static import names no file in the repository: nothing to link.
        assert not any("Collections" in t for t in refs | calls)
    finally:
        store.close()


def test_class_used_only_through_a_static_import_is_not_dead(tmp_path):
    dead = _dead(tmp_path, STATIC_FILES)
    assert "Slugs" not in dead  # named import of a method
    assert "Limits" not in dead  # named import of a constant
    assert "Everything" not in dead  # wildcard static import
    assert "Slugs.slugify" not in dead
    # Imported members and untouched ones are told apart.
    assert "Slugs.unusedStatic" in dead
    assert "Orphan" in dead


def test_a_method_declared_in_the_class_hides_the_static_import(tmp_path):
    files = {
        SRC + "util/Slugs.java": STATIC_FILES[SRC + "util/Slugs.java"],
        SRC + "app/Local.java": """package shop.app;
import static shop.util.Slugs.slugify;
public class Local {
  String slugify(String s) { return s; }
  public String go() { return slugify("x"); }
}
""",
    }
    _, store, parsed = _build(tmp_path, files)
    try:
        _, edges = parsed["Local.java"]
        assert not any(
            e.kind == "CALLS" and e.target.endswith("Slugs.slugify") for e in edges
        )
    finally:
        store.close()


# --- 5: calls through typed fields and record accessors ---------------------------

TYPED_FILES = {
    SRC + "Pricing.java": """package shop;
public class Pricing {
  public long total() { return 1L; }
  public long discount() { return 0L; }
  public long audit() { return 0L; }
}
""",
    SRC + "Window.java": """package shop;
public record Window(int idsPerFile, String label) {
  public int idsPerFile() { return idsPerFile; }
}
""",
    SRC + "Limits.java": """package shop;
public record Limits(Pricing pricing, int ceiling) {
  public long cost() { return pricing.total(); }
}
""",
    SRC + "Holder.java": """package shop;
public class Holder {
  public static class Settings {
    public int depth() { return 3; }
  }
}
""",
    SRC + "billing/Checkout.java": """package shop.billing;
import shop.*;
public class Checkout {
  private final Pricing pricing;
  private final Window window;
  private final Holder.Settings settings;
  private Limits limits;
  public Checkout(Pricing pricing, Window window, Holder.Settings settings) {
    this.pricing = pricing;
    this.window = window;
    this.settings = settings;
  }
  public long run() {
    int depth = settings.depth();
    return this.pricing.total() + pricing.discount() + window.idsPerFile() + depth;
  }
}
""",
}


def test_calls_through_fields_resolve_to_the_declared_type(tmp_path):
    repo, store, parsed = _build(tmp_path, TYPED_FILES)
    try:
        _, edges = parsed["Checkout.java"]
        pricing = str(repo / SRC / "Pricing.java")
        window = str(repo / SRC / "Window.java")
        holder = str(repo / SRC / "Holder.java")
        calls = {e.target for e in edges if e.kind == "CALLS"}
        assert f"{pricing}::Pricing.total" in calls  # this.field.method()
        assert f"{pricing}::Pricing.discount" in calls  # field.method()
        assert f"{window}::Window.idsPerFile" in calls  # record accessor, wildcard import
        assert f"{holder}::Holder.Settings.depth" in calls  # nested type
    finally:
        store.close()


def test_record_component_used_as_a_typed_field(tmp_path):
    repo, store, parsed = _build(tmp_path, TYPED_FILES)
    try:
        _, edges = parsed["Limits.java"]
        pricing = str(repo / SRC / "Pricing.java")
        assert f"{pricing}::Pricing.total" in {
            e.target for e in edges if e.kind == "CALLS"
        }
    finally:
        store.close()


def test_methods_reached_through_fields_are_not_dead(tmp_path):
    dead = _dead(tmp_path, TYPED_FILES)
    assert "Pricing.total" not in dead
    assert "Pricing.discount" not in dead
    assert "Holder.Settings.depth" not in dead
    # The one method nothing calls is still reported.
    assert "Pricing.audit" in dead


def test_ambiguous_receiver_stays_unresolved(tmp_path):
    files = {
        SRC + "A.java": "package shop;\npublic class A { public void ping() {} }\n",
        SRC + "B.java": "package shop;\npublic class B { public void ping() {} }\n",
        SRC + "Use.java": """package shop;
public class Use {
  private final Object thing;
  public Use(Object thing) { this.thing = thing; }
  public void go() { thing.ping(); }
}
""",
    }
    _, store, parsed = _build(tmp_path, files)
    try:
        _, edges = parsed["Use.java"]
        assert not any(
            e.kind == "CALLS" and e.target.endswith(("A.ping", "B.ping")) for e in edges
        )
    finally:
        store.close()

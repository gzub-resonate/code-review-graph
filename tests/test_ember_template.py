"""Structural scan of Ember ``.hbs`` templates: what is read, what is skipped, the bounds."""

import time

import pytest

from code_review_graph import ember_template as et


def _found(text: str) -> list[tuple[str, str]]:
    return [(i.ref, i.name) for i in et.scan(text)]


def test_angle_bracket_components_are_dasherized_and_nested_names_use_slashes() -> None:
    assert _found("<FooBar /><Foo::Bar></Foo::Bar><HTMLThing/>") == [
        ("component", "foo-bar"),
        ("component", "foo/bar"),
        ("component", "html-thing"),
    ]


def test_html_elements_builtins_and_yielded_paths_are_not_components() -> None:
    assert _found("<div><span/><Input/><LinkTo @route='x'/><M.Item/><this.Dyn/><@arg/></div>") == []


def test_curly_invocations_and_block_form() -> None:
    text = "{{foo-bar}}{{#baz-block x=1}}y{{/baz-block}}{{qux arg}}{{plain}}{{ns/thing}}"
    assert _found(text) == [
        ("invocable", "foo-bar"),
        ("component", "baz-block"),
        ("invocable", "qux"),
        ("invocable", "ns/thing"),
    ]


def test_builtins_and_property_reads_are_skipped() -> None:
    text = "{{if a b}}{{#each xs as |x|}}{{x}}{{/each}}{{yield}}{{outlet}}{{on 'click' go}}{{@arg}}"
    assert _found(text) == []


def test_block_params_shadow_names_everywhere_in_the_file() -> None:
    text = "{{#let (hash a=1) as |item-row|}}{{item-row 1}}{{/let}}"
    assert _found(text) == []


def test_modifiers_in_element_position_only() -> None:
    text = "<div {{did-thing 'x'}} class={{my-helper y}} {{on 'click' go}}></div>"
    assert _found(text) == [("modifier", "did-thing"), ("invocable", "my-helper")]


def test_subexpressions_are_helpers_and_builtins_are_skipped() -> None:
    assert sorted(_found("{{x-y (fmt-date d) (hash a=1) (concat 'a' 'b')}}")) == [
        ("helper", "fmt-date"),
        ("invocable", "x-y"),
    ]


def test_component_helper_literal_is_a_name_but_dynamic_forms_are_not() -> None:
    text = "{{component 'lit-one'}}{{component this.which}}{{helper 'h-two'}}(modifier 'm-three')"
    assert _found(text) == [("component", "lit-one"), ("helper", "h-two")]
    assert sorted(_found("{{x-y (component 'in-sub')}}")) == [
        ("component", "in-sub"),
        ("invocable", "x-y"),
    ]


def test_comments_escapes_and_raw_blocks_are_not_live() -> None:
    text = (
        "{{!-- {{ghost-one}} --}}{{! {{ghost-two}} }}<!-- <GhostThree/> -->"
        "\\{{ghost-four}}{{{{raw}}}} {{ghost-five}} {{{{/raw}}}}{{real-one}}"
    )
    assert _found(text) == [("invocable", "real-one")]


def test_unusual_syntax_does_not_confuse_the_scanner() -> None:
    text = (
        '{{t "a}}b"}}{{~foo-bar~}}{{{triple-stash x}}}'
        "<a href={{my-url x}} title='{{in-quote y}}' data-x=\"{{if a 'b'}}\">"
        "{{else if cond}}{{else}}<:named-block>x</:named-block> ...attributes"
    )
    result = _found(text)
    assert ("invocable", "t") in result
    assert ("invocable", "foo-bar") in result
    assert ("invocable", "triple-stash") in result
    assert ("invocable", "my-url") in result
    assert ("invocable", "in-quote") in result
    assert not any(name in ("named-block", "else", "cond") for _, name in result)


def test_repeats_collapse_with_a_count_and_the_first_line() -> None:
    result = et.scan("\n<FooBar/>\n{{foo-bar}}\n<FooBar/>")
    (angle,) = [i for i in result if i.ref == "component"]
    assert (angle.line, angle.count) == (2, 2)


def test_no_text_or_literal_value_is_returned() -> None:
    result = et.scan('<FooBar @title="top-secret-value" class="secret-class">secret body</FooBar>')
    blob = repr(result)
    assert "secret" not in blob and "top-secret" not in blob


def test_oversized_template_is_not_scanned() -> None:
    assert et.scan("{{foo-bar}}" + " " * et.MAX_TEMPLATE_BYTES) == []


def test_distinct_names_are_capped() -> None:
    text = "".join(f"{{{{name-{n}}}}}" for n in range(et.MAX_INVOCATIONS + 500))
    assert len(et.scan(text)) == et.MAX_INVOCATIONS


# Hostile inputs: each must finish quickly and keep its output bounded.
_HOSTILE = {
    "unclosed_subexpression_openers": "{{" + "(a " * 300_000,
    "lt_only": "<" * 900_000,
    "lt_then_mustache_tail": "<" * 100_000 + "{{foo-bar}}",
    "mustache_openers_only": "{{" * 400_000,
    "comment_openers_only": "{{!" * 300_000,
    "deep_parentheses": "{{" + "(a " * 5_000 + ")" * 5_000 + "}}",
    "unclosed_tags": "<div " * 100_000,
    "unclosed_quotes_in_tags": "<a b='{{" * 50_000,
    "raw_openers": "{{{{" * 200_000,
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_hostile_input_finishes_quickly_with_bounded_output(name: str) -> None:
    started = time.perf_counter()
    result = et.scan(_HOSTILE[name])
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"{name} took {elapsed:.1f}s"
    assert len(result) <= et.MAX_INVOCATIONS


def test_unterminated_mustache_never_uses_the_rest_of_the_file_as_a_body() -> None:
    # No closer within the span: nothing after the opener is read as an invocation.
    text = "{{ " + "x " * (et._MAX_SPAN) + " (late-helper 1)"
    assert _found(text) == []
    assert et._skip_mustache("{{ never closes", 0) == et.UNTERMINATED


def test_work_budget_stops_a_file_that_is_all_long_spans() -> None:
    chunk = "{{" + "a " * (et._MAX_SPAN // 2 - 10)
    started = time.perf_counter()
    et.scan(chunk * 60)
    assert time.perf_counter() - started < 3.0

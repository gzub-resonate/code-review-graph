"""The ``terraform``, ``workflow``, ``stepfunction`` and ``awssdk`` origins page their rows.

A cut list used to end at ``max_results`` rows with ``truncated`` set and no way to read the
rest. It now carries ``next_cursor``; passing it back returns the next page. The first page
is what a call without a cursor returned before. A slow call is also logged as a warning,
because the server does not configure INFO logging.
"""

from __future__ import annotations

import logging

import pytest

from code_review_graph.paging import CursorError
from code_review_graph.tools import registry_tools as rt
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import registry as registry  # noqa: F401
from .test_cross_repo_speedups import world as world  # noqa: F401
from .test_service_aliases_infra_join import infra_world as infra_world  # noqa: F401


def read_all_pages(page_size: int, **query):
    """Every row of a query, read ``page_size`` at a time, and the pages it took."""
    rows: list = []
    pages = 0
    cursor = None
    while True:
        reply = cross_repo_requests_func(max_results=page_size, cursor=cursor, **query)
        rows.extend(reply["results"])
        pages += 1
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, pages, reply
        assert pages < 200, "paging did not end"


class TestStepFunctionPages:
    def test_a_cut_list_carries_a_cursor_and_the_pages_add_up(self, world):  # noqa: F811
        everything = cross_repo_requests_func(origin="stepfunction", max_results=100)
        assert everything["total"] >= 3, "the fixture needs several rows to page"
        assert "next_cursor" not in everything and everything["truncated"] is False

        first = cross_repo_requests_func(origin="stepfunction", max_results=2)
        assert len(first["results"]) == 2
        assert first["truncated"] is True and first["next_cursor"]
        assert first["total"] == everything["total"]
        assert first["results"] == everything["results"][:2]
        assert "offset" not in first

        rows, pages, last = read_all_pages(2, origin="stepfunction")
        assert rows == everything["results"]
        assert pages == -(-everything["total"] // 2)
        assert last["truncated"] is False and "next_cursor" not in last
        assert last["offset"] > 0

    def test_a_page_after_the_first_names_its_range(self, world):  # noqa: F811
        first = cross_repo_requests_func(origin="stepfunction", max_results=2)
        second = cross_repo_requests_func(
            origin="stepfunction", max_results=2, cursor=first["next_cursor"],
        )
        assert second["offset"] == 2
        assert f"showing 3-{2 + len(second['results'])} of {second['total']}" in second["summary"]

    def test_a_cursor_for_another_query_is_refused(self, world):  # noqa: F811
        first = cross_repo_requests_func(origin="stepfunction", max_results=2)
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="stepfunction", max_results=2, service="validateOrder",
                cursor=first["next_cursor"],
            )
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="terraform", max_results=2, cursor=first["next_cursor"],
            )

    def test_a_garbled_cursor_is_refused(self, world):  # noqa: F811
        with pytest.raises(CursorError):
            cross_repo_requests_func(origin="stepfunction", cursor="not-a-cursor")

    def test_a_filtered_query_pages_over_the_filtered_rows(self, world):  # noqa: F811
        everything = cross_repo_requests_func(origin="stepfunction", max_results=100)
        wanted = [r for r in everything["results"] if r["target"].startswith("lambda")]
        assert len(wanted) >= 2
        rows, _pages, _last = read_all_pages(1, origin="stepfunction", route="lambda")
        assert rows == wanted


class TestAwsSdkPages:
    def test_the_pages_add_up_to_the_whole_list(self, infra_world):  # noqa: F811
        everything = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=100,
        )
        assert everything["total"] >= 2
        first = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
        )
        assert first["truncated"] is True and first["next_cursor"]
        rows, _pages, last = read_all_pages(1, origin="awssdk", repos=["inventory-svc"])
        assert rows == everything["results"]
        assert last["truncated"] is False

    def test_the_infrastructure_block_comes_with_the_first_page_only(self, infra_world):  # noqa: F811
        first = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
        )
        second = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
            cursor=first["next_cursor"],
        )
        assert "infrastructure" in first and "infrastructure_note" not in first
        assert "infrastructure" not in second
        assert "first page only" in second["infrastructure_note"]
        # Paging itself is untouched: the cursor chain still reaches the end.
        assert second["offset"] == 1 and second["results"]

    def test_a_continuation_does_not_build_the_block(self, infra_world, monkeypatch):  # noqa: F811
        first = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
        )
        calls = []
        monkeypatch.setattr(
            rt, "_attach_infrastructure", lambda *a, **k: calls.append(1),
        )
        cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
            cursor=first["next_cursor"],
        )
        assert calls == []

    def test_a_stale_cursor_is_still_refused(self, infra_world):  # noqa: F811
        first = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=1,
        )
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="awssdk", repos=["other"], max_results=1, cursor=first["next_cursor"],
            )


class TestSlowCallIsLogged:
    @staticmethod
    def timing_records(caplog):
        return [r for r in caplog.records if r.getMessage().startswith("cross-repo ")]

    def test_a_fast_call_is_logged_below_warning(self, world, caplog):  # noqa: F811
        with caplog.at_level(logging.DEBUG, logger=rt.logger.name):
            cross_repo_requests_func(origin="stepfunction")
        records = self.timing_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "stepfunction" in records[0].getMessage()
        assert "read " in records[0].getMessage()

    def test_a_slow_call_is_a_warning(self, world, caplog, monkeypatch):  # noqa: F811
        monkeypatch.setenv("CRG_CROSS_REPO_SLOW_SECONDS", "0")
        with caplog.at_level(logging.WARNING, logger=rt.logger.name):
            cross_repo_requests_func(origin="stepfunction")
        records = self.timing_records(caplog)
        assert len(records) == 1 and records[0].levelno == logging.WARNING

    @pytest.mark.parametrize("raw", [None, "", "  ", "soon", "-1", "nan-ish"])
    def test_a_missing_or_bad_threshold_uses_the_default(self, raw, monkeypatch):
        if raw is None:
            monkeypatch.delenv("CRG_CROSS_REPO_SLOW_SECONDS", raising=False)
        else:
            monkeypatch.setenv("CRG_CROSS_REPO_SLOW_SECONDS", raw)
        assert rt._slow_call_seconds() == rt.DEFAULT_SLOW_CALL_SECONDS

    def test_a_set_threshold_is_used(self, monkeypatch):
        monkeypatch.setenv("CRG_CROSS_REPO_SLOW_SECONDS", "2.5")
        assert rt._slow_call_seconds() == 2.5


class TestInfrastructureNamesWhatItSkipped:
    def test_repos_left_out_by_the_cap_are_named(self, world, monkeypatch):  # noqa: F811
        from code_review_graph import infra_join

        everything = cross_repo_requests_func(origin="stepfunction", max_results=100)
        assert everything["infrastructure"]["truncated"] is False
        assert "repos_not_checked" not in everything["infrastructure"]

        monkeypatch.setattr(infra_join, "MAX_REPOS", 1)
        capped = cross_repo_requests_func(origin="stepfunction", max_results=100)
        block = capped["infrastructure"]
        assert block["truncated"] is True and block["repos_checked"] == 1
        assert block["repos_not_checked_total"] == 3
        assert len(block["repos_not_checked"]) == 3
        # Asking again with the named repos, under the normal cap, covers them.
        monkeypatch.setattr(infra_join, "MAX_REPOS", 20)
        again = cross_repo_requests_func(
            origin="stepfunction", repos=block["repos_not_checked"], max_results=100,
        )
        assert again["infrastructure"]["truncated"] is False


class TestFilteredInfrastructure:
    """A target filter scopes the infrastructure block to the repos the rows belong to."""

    def test_no_rows_means_no_unrelated_infrastructure_block(self, infra_world):  # noqa: F811
        reply = cross_repo_requests_func(
            origin="stepfunction", service="no-such-target", repos=["inventory-svc"],
        )
        assert reply["total"] == 0 and reply["results"] == []
        assert "infrastructure" not in reply
        assert "no rows matched" in reply["infrastructure_note"]
        assert "omitted" in reply["infrastructure_note"]
        assert reply["truncated"] is False

    def test_a_filter_with_rows_describes_only_the_repos_of_those_rows(self, world):  # noqa: F811
        everything = cross_repo_requests_func(origin="stepfunction", max_results=100)
        row = everything["results"][0]
        reply = cross_repo_requests_func(
            origin="stepfunction", service=row["target_name"], max_results=100,
        )
        assert reply["total"] >= 1
        owners = {r["repo"] for r in reply["results"]} | {
            r["target_repo"] for r in reply["results"] if r.get("target_repo")
        }
        described = set(reply.get("infrastructure", {}).get("repos", {}))
        assert described <= owners
        assert reply["infrastructure"]["repos_checked"] <= len(owners)

    def test_an_unfiltered_call_keeps_the_block_over_the_selected_repos(self, infra_world):  # noqa: F811
        reply = cross_repo_requests_func(origin="awssdk", repos=["inventory-svc"])
        assert "inventory-svc" in reply["infrastructure"]["repos"]

    def test_truncated_says_what_was_cut(self, infra_world, monkeypatch):  # noqa: F811
        monkeypatch.setattr(rt.ij, "MAX_REPOS", 1)
        reply = cross_repo_requests_func(origin="terraform")
        assert reply["truncated"] is True
        assert "infrastructure block" in reply["truncated_reason"]
        clean = cross_repo_requests_func(origin="awssdk", repos=["inventory-svc"])
        assert clean["truncated"] is False and "truncated_reason" not in clean

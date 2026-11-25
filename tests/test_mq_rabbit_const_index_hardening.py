"""The repository constants index reads only plain, bounded files and survives deep nesting."""

import os
import signal
from pathlib import Path

import pytest

from code_review_graph import messaging_edges
from code_review_graph.parser import CodeParser

NAMES = """
package com.example.shared;
public final class OrderNames {
    public static final String EXCHANGE = "orders.exchange";
    public static final String CREATED = "orders.created";
}
"""

PUBLISHER = """
package com.example.pub;
import com.example.shared.OrderNames;
class OrderPublisher {
    private RabbitTemplate rabbit;
    void go(Object p) {
        rabbit.convertAndSend(OrderNames.EXCHANGE, OrderNames.CREATED, p);
    }
}
"""


def _publisher_targets(root: Path) -> set[str]:
    parser = CodeParser(root)
    _, edges = parser.parse_file(root / "pub" / "OrderPublisher.java")
    return {e.target for e in edges if e.kind == "PRODUCES"}


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_plain_file_resolves_the_exchange(tmp_path: Path) -> None:
    # The control for the symlink case below: the same constants as a regular file resolve.
    _write(tmp_path, "shared/OrderNames.java", NAMES)
    _write(tmp_path, "pub/OrderPublisher.java", PUBLISHER)
    assert "external:rabbitmq:orders.exchange" in _publisher_targets(tmp_path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinked_java_file_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    repo = tmp_path / "repo"
    _write(outside, "OrderNames.java", NAMES)
    _write(repo, "pub/OrderPublisher.java", PUBLISHER)
    (repo / "shared").mkdir()
    os.symlink(outside / "OrderNames.java", repo / "shared" / "OrderNames.java")
    assert "external:rabbitmq:orders.exchange" not in _publisher_targets(repo)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_fifo_named_like_a_java_file_does_not_block(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "Pipe.java")

    def _timeout(signum, frame):  # pragma: no cover - only runs when the read would hang
        raise TimeoutError("opening a FIFO blocked")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        assert messaging_edges._read_const_file(str(tmp_path / "Pipe.java"), parser=None) is None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
def test_device_reporting_size_zero_is_not_read() -> None:
    # os.stat reports size 0 for a device, so a size check alone would let it through.
    assert messaging_edges._read_const_file("/dev/zero", parser=None) is None


def test_file_over_the_size_cap_is_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(messaging_edges, "_MAX_CONST_FILE_BYTES", 50)
    _write(tmp_path, "shared/OrderNames.java", NAMES)
    assert messaging_edges._read_const_file(
        str(tmp_path / "shared" / "OrderNames.java"), parser=None,
    ) is None


def test_deeply_nested_classes_do_not_break_the_build(tmp_path: Path) -> None:
    depth = 1500  # two frames per level: well past Python's default recursion limit
    deep = "package com.example.deep;\n" + "".join(f"class C{i} {{\n" for i in range(depth))
    # A constant in the innermost class, so the file passes the cheap pre-filter and is walked.
    deep += 'static final String LEAF = "leaf";\n' + "}\n" * depth
    _write(tmp_path, "shared/OrderNames.java", NAMES)
    _write(tmp_path, "shared/Deep.java", deep)
    _write(tmp_path, "pub/OrderPublisher.java", PUBLISHER)
    # No RecursionError, and the shallow constants still resolve.
    assert "external:rabbitmq:orders.exchange" in _publisher_targets(tmp_path)

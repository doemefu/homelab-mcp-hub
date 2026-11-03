"""The hand-written Prometheus exposition (spec 080 §9.7 rev 4.17, §10.1)."""

import re
from typing import Any, get_args

from mcp_hub.errors import ErrorCode
from mcp_hub.health import HealthStatus
from mcp_hub.metrics import REJECTION_CHECKS, STATUSES, TOOL_NAMES, TOOL_OUTCOMES, Metrics

SERIES = re.compile(r'^(mcp_hub_[a-z_]+)\{((?:[a-z_]+="(?:[^"\\\n]|\\.)*",?)+)\} (0|[1-9][0-9]*)$')


def parse(text: str) -> tuple[list[str], dict[str, int]]:
    """Comment lines and series; asserts the text-format grammar on every line."""
    assert text.endswith("\n")
    comments: list[str] = []
    series: dict[str, int] = {}
    for line in text[:-1].split("\n"):
        if line.startswith("#"):
            comments.append(line)
            continue
        match = SERIES.match(line)
        assert match is not None, line
        name = line.rsplit(" ", 1)[0]
        assert name not in series, f"duplicate series {name}"
        series[name] = int(match.group(3))
    return comments, series


def test_enums() -> None:
    assert ("ok", "partial", "error", *get_args(ErrorCode)) == TOOL_OUTCOMES
    assert get_args(HealthStatus) == STATUSES
    assert (len(TOOL_NAMES), len(TOOL_OUTCOMES), len(REJECTION_CHECKS)) == (8, 13, 16)


def test_every_counter_series_starts_at_zero() -> None:
    comments, series = parse(Metrics().render([]))
    tools = [name for name in series if name.startswith("mcp_hub_tool_calls_total")]
    checks = [name for name in series if name.startswith("mcp_hub_token_rejections_total")]
    assert (len(tools), len(checks)) == (104, 16)
    assert set(series.values()) == {0}
    for family, kind in (
        ("mcp_hub_account_status", "gauge"),
        ("mcp_hub_tool_calls_total", "counter"),
        ("mcp_hub_token_rejections_total", "counter"),
    ):
        assert [c for c in comments if c.startswith(f"# TYPE {family} ")] == [f"# TYPE {family} {kind}"]
        assert len([c for c in comments if c.startswith(f"# HELP {family} ")]) == 1


def test_status_gauge_is_one_hot() -> None:
    statuses = [("icloud", "mail", "ok"), ("icloud", "calendar", "unknown"), ("old", "mail", "disabled")]
    _, series = parse(Metrics().render(statuses))
    for account, capability, current in statuses:
        values = {
            status: series[f'mcp_hub_account_status{{account="{account}",capability="{capability}",status="{status}"}}']
            for status in STATUSES
        }
        assert values == {status: int(status == current) for status in STATUSES}


def test_counts_and_clamps() -> None:
    metrics = Metrics()
    metrics.count_tool_call("get_thread", "partial")
    metrics.count_tool_call("get_thread", "partial")
    metrics.count_tool_call("get_thread", "made-up")  # type: ignore[arg-type]  # a value outside the enum
    metrics.count_tool_call("made_up_tool", "ok")  # type: ignore[arg-type]  # a tool outside the enum
    metrics.count_rejection("body_size")
    metrics.count_rejection("made-up")  # type: ignore[arg-type]  # a check outside the enum
    _, series = parse(metrics.render([]))
    assert series['mcp_hub_tool_calls_total{tool="get_thread",outcome="partial"}'] == 2
    assert series['mcp_hub_tool_calls_total{tool="get_thread",outcome="error"}'] == 1
    assert series['mcp_hub_token_rejections_total{check="body_size"}'] == 1
    assert series['mcp_hub_token_rejections_total{check="internal"}'] == 1
    assert sum(series.values()) == 5
    assert not any("made" in name for name in series)


def test_label_values_are_escaped() -> None:
    hostile: Any = 'a"b\\c\nd'
    _, series = parse(Metrics().render([(hostile, "mail", "ok")]))
    assert 'mcp_hub_account_status{account="a\\"b\\\\c\\nd",capability="mail",status="ok"}' in series

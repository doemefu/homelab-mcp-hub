"""ICS fixture loader: CRLF line endings and the zero-width placeholder (no invisible character is committed)."""

from pathlib import Path

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ics"
NAMES = (
    "dst-weekly.ics",
    "standup-overrides.ics",
    "cancelled.ics",
    "allday.ics",
    "cross-zone.ics",
    "floating.ics",
    "window-edge.ics",
    "hostile.ics",
    "rdate.ics",
)


def load(name: str) -> bytes:
    text = (FIXTURES / name).read_text(encoding="utf-8").replace("{ZWSP}", "\u200b")
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")

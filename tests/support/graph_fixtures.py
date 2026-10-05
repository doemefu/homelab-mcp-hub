"""Canned Microsoft Graph mail answers for unit tests (neutral example.test data only)."""

INBOX_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAD"


def message(n: int, **extra: object) -> dict[str, object]:
    return {
        "id": f"AAMkMSG{n:04d}=",
        "receivedDateTime": f"2026-09-29T06:{n % 60:02d}:00Z",
        "isRead": False,
        "hasAttachments": False,
        "from": {"emailAddress": {"name": f"Sender {n}", "address": f"s{n}@example.test"}},
        "subject": f"Subject {n}",
        "bodyPreview": f"Preview {n}",
    } | extra


class FakeTokens:
    """Hands out AT-SENTINEL-1, -2, … ; counts refresh-forcing invalidations."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    def access_token(self, deadline: float) -> str:
        self.issued += 1
        return f"AT-SENTINEL-{self.issued}"

    def invalidate(self) -> None:
        self.invalidated += 1

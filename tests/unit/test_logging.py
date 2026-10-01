import json
import logging
import sys
import warnings

import pytest

from mcp_hub.logging import JsonFormatter, apply_logger_levels, configure_logging, log_event

THIRD_PARTY = ["httpx2", "httpcore2", "mcp", "imapclient", "uvicorn"]


def test_third_party_loggers_are_capped_even_at_debug() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("mcp_hub").level == logging.DEBUG
    for name in THIRD_PARTY:
        assert logging.getLogger(name).level == logging.WARNING, name
    # SDK WARNING lines carry raw Host/Origin values (spec 080 §9.7).
    assert logging.getLogger("mcp.server.transport_security").level == logging.ERROR


def test_levels_survive_sdk_basic_config() -> None:
    from mcp.server.mcpserver import MCPServer

    configure_logging("INFO")
    MCPServer(name="probe")  # calls logging.basicConfig(level="INFO") (spec 080 §9.7)
    apply_logger_levels("INFO")
    assert logging.getLogger().level == logging.WARNING
    assert len(logging.getLogger().handlers) == 1


def test_json_line_has_fields_and_no_traceback() -> None:
    record = logging.LogRecord("mcp_hub.x", logging.ERROR, __file__, 1, "token_rejected", None, None)
    record.fields = {"check": "audience"}
    try:
        raise ValueError("secret-sentinel")
    except ValueError:
        record.exc_info = sys.exc_info()
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "token_rejected"
    assert line["level"] == "ERROR"
    assert line["check"] == "audience"
    assert line["exception"] == "ValueError"
    assert "secret-sentinel" not in json.dumps(line)


def test_log_event_passes_fields(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")
    logger = logging.getLogger("mcp_hub.test")
    logger.addHandler(caplog.handler)
    log_event(logger, logging.INFO, "request", status=401)
    assert caplog.records[-1].fields == {"status": 401}


def test_third_party_message_text_is_dropped() -> None:
    record = logging.getLogger("httpx2").makeRecord(
        "httpx2", logging.WARNING, __file__, 1, "GET https://x.example.org/secret", None, None
    )
    line = JsonFormatter().format(record)
    assert json.loads(line)["event"] == "third_party_log"
    assert "x.example.org" not in line


def test_python_warnings_become_json_lines() -> None:
    captured: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = Collect()
    logging.getLogger("py.warnings").addHandler(handler)
    try:
        with warnings.catch_warnings():
            # pytest installs its own warnings hook per test; start capture fresh inside this block.
            logging.captureWarnings(False)
            configure_logging("INFO")
            warnings.simplefilter("always")
            warnings.warn("secret-sentinel", DeprecationWarning, stacklevel=1)
    finally:
        logging.getLogger("py.warnings").removeHandler(handler)
    assert captured
    assert captured[-1].name == "py.warnings"
    line = JsonFormatter().format(captured[-1])
    assert json.loads(line)["event"] == "third_party_log"
    assert "secret-sentinel" not in line


def test_pinned_loggers_are_the_installed_third_party_packages() -> None:
    # spec 080 rev. 4.4 §9.7: no pins for packages that are not installed (caldav, niquests; D60)
    from mcp_hub.logging import PINNED_WARNING

    assert sorted(PINNED_WARNING) == sorted(THIRD_PARTY)

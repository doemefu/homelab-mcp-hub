"""Re-review R1/R4: the HTML converter on malformed input, through the same check the image smoke test runs."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _check() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "html_scaling_check.py"
    spec = importlib.util.spec_from_file_location("html_scaling_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# CPython's html.parser is quadratic on some malformed shapes in older 3.13 patch releases (3.13.5: 44 s for
# 256 KiB of "<x"); 3.13.15, the version of the production image and the CI runners, scales linearly. On older
# interpreters the production check in scripts/smoke_image.sh is the evidence.
@pytest.mark.skipif(sys.version_info < (3, 13, 15), reason="stdlib html.parser is quadratic before 3.13.15")
@pytest.mark.parametrize("shape", list(_check().SHAPES))
def test_html_to_text_scales_linearly_on_malformed_input(shape: str) -> None:
    check = _check()
    build = check.SHAPES[shape]
    small, large = check.elapsed(build(check.SMALL)), check.elapsed(build(check.LARGE))
    assert check.scales_linearly(small, large), (shape, small, large)


def test_unterminated_comment_or_declaration_stays_hidden() -> None:
    assert _check().hidden_text_leaks() == []

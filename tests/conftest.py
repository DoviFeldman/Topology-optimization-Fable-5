import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import make_test_shapes as shapes  # noqa: E402


@pytest.fixture(scope="session")
def shape_dir(tmp_path_factory) -> Path:
    """Generate the standard test shapes once per test session."""
    out = tmp_path_factory.mktemp("shapes")
    shapes.make_box().export(out / "box.stl")
    shapes.make_box_with_hole().export(out / "box_with_hole.stl")
    shapes.make_l_bracket().export(out / "l_bracket.stl")
    return out

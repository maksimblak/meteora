from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.api.support import Panel, make_panel


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[Panel]:
    instance = make_panel(tmp_path)
    yield instance
    instance.repositories.close()

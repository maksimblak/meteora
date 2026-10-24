"""F3 (C5, interfaces ``ports.persistence``): the api reads the decision journal through a
protocol; database entities, SQL and indexes stay inside ``src.persistence``."""

import ast
from pathlib import Path

import pytest

API = Path("src/api")
FORBIDDEN = ("sqlalchemy", "src.persistence.models", "src.persistence.postgres")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("path", sorted(API.glob("*.py")), ids=lambda item: item.name)
def test_the_api_imports_no_database_entities_or_sql(path: Path) -> None:
    modules = imported_modules(path)

    leaked = sorted(
        name
        for name in modules
        for forbidden in FORBIDDEN
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert leaked == []

from pathlib import Path

import yaml
from sqlalchemy.engine import make_url


def test_compose_exposes_reproducible_shadow_and_check_stacks() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"postgres", "api", "worker", "executor", "check"} <= services.keys()
    assert services["postgres"]["healthcheck"]
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["executor"]["profiles"] == ["live"]
    assert services["check"]["profiles"] == ["test"]
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]
    assert "ports" not in services["executor"]
    assert services["executor"]["expose"] == ["3000"]
    assert "DATABASE_URL_FILE" in "\n".join(services["check"]["command"])

    api_environment = services["api"]["environment"]
    assert "SOLANA_RPC_URL" not in api_environment
    assert "WALLET_KEYPAIR_FILE" not in api_environment
    assert "EXECUTOR_INTERNAL_TOKEN" not in api_environment

    assert Path("Dockerfile").is_file()
    assert Path("Dockerfile.executor").is_file()


def test_check_migrates_and_tests_its_own_database_next_to_the_working_one() -> None:
    """The check may run beside a live stack (F6): same postgres, never the working database."""

    services = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))["services"]
    check = services["check"]
    tests_database = make_url(check["environment"]["TEST_DATABASE_URL"])

    for name in ("migrate", "api", "worker"):
        working = make_url(services[name]["environment"]["DATABASE_URL"])
        assert (tests_database.host, tests_database.port) == (working.host, working.port)
        assert tests_database.database != working.database

    # ``alembic upgrade head`` reads DATABASE_URL (via DATABASE_URL_FILE): the check's own one.
    assert make_url(check["environment"]["DATABASE_URL"]) == tests_database
    script = "\n".join(check["command"])
    assert script.index("tests.check_database") < script.index("alembic upgrade head")


def test_example_environment_contains_names_but_no_values() -> None:
    assignments = {
        name: value
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split("=", maxsplit=1)]
    }

    assert all(value == "" for value in assignments.values())
    assert {
        "APP_OPERATOR_TOKEN",
        "APP_OPERATOR_TOKEN_FILE",
        "APP_SESSION_SECRET",
        "APP_SESSION_SECRET_FILE",
        "DATABASE_URL",
        "DATABASE_URL_FILE",
        "WALLET_KEYPAIR_FILE",
        "EXECUTOR_INTERNAL_TOKEN",
        "EXECUTOR_INTERNAL_TOKEN_FILE",
    } <= assignments.keys()

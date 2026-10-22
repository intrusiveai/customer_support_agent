from pathlib import Path
import subprocess

import pytest

from customer_support.cli import main
from customer_support.config import Config
from customer_support.storage import connect


def test_public_quickstart_initializes_fictional_data_without_aws(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(Path("config.example.toml").read_text())
    assert main(["--config", str(config_path), "init-db", "--demo"]) == 0
    config = Config.load(config_path)
    with connect(config.database_path) as db:
        assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 2
    assert subprocess.run([".venv/bin/support-agent", "--help"], capture_output=True).returncode == 0


@pytest.mark.parametrize("values", [{"s3_list_limit": 1001}, {"retrieval_results": 101},
                                   {"read_timeout_seconds": 0}, {"summary_max_chars": True},
                                   {"refund_eligible_statuses": ["pending"]}])
def test_configuration_rejects_invalid_limits(values):
    with pytest.raises(ValueError):
        Config(model_id="model", knowledge_base_id="kb", summary_bucket="bucket", **values)

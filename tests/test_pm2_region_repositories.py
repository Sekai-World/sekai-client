"""Keep PM2 updater repositories aligned with the regional topology."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("region", "repository"),
    [
        ("JP", "sekai-master-db-diff"),
        ("EN", "sekai-master-db-en-diff"),
        ("TW", "sekai-master-db-tc-diff"),
        ("KR", "sekai-master-db-kr-diff"),
    ],
)
def test_user_information_template_uses_regional_repository(region, repository):
    template = ROOT / "deployment" / "pm2" / "examples" / (
        f"updateUserInformation{region}.yaml.example"
    )

    config = yaml.safe_load(template.read_text())

    assert (
        config["apps"][0]["env"]["GIT_FOLDER_SEKAI_MASTER_DB_DIFF"]
        == repository
    )

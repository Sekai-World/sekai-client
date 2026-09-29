"""Keep PM2 updater repositories aligned with the regional topology."""

from pathlib import Path

import pytest
import yaml

import check_update

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
    template = (
        ROOT
        / "deployment"
        / "pm2"
        / "examples"
        / (f"updateUserInformation{region}.yaml.example")
    )

    config = yaml.safe_load(template.read_text())

    assert config["apps"][0]["env"]["GIT_FOLDER_SEKAI_MASTER_DB_DIFF"] == repository


def test_check_update_templates_set_distinct_supported_regions(monkeypatch):
    monkeypatch.delenv("CHECK_UPDATE_DAILY_DUE_STATE_PATH", raising=False)
    template_regions = {
        "JP": "jp",
        "EN": "en",
        "TW": "tw",
        "KR": "kr",
        "CN": "cn",
    }

    marker_paths = []
    for template_region, configured_region in template_regions.items():
        template = (
            ROOT
            / "deployment"
            / "pm2"
            / "examples"
            / f"checkUpdate{template_region}.yaml.example"
        )
        config = yaml.safe_load(template.read_text())
        assert config["apps"][0]["env"]["SEKAI_REGION"] == configured_region
        marker_paths.append(check_update._daily_due_state_path(configured_region))

    assert len(set(marker_paths)) == len(template_regions)

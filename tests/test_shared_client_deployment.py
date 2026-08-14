"""Contract tests for formal shared-client PM2 templates."""

import re
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "deployment" / "pm2" / "examples"
CANARY_DIR = Path(__file__).resolve().parents[1] / "deployment" / "pm2" / "canary"
EXPECTED_PORTS = {"jp": 39390, "tw": 39391, "en": 39392, "kr": 39393}
FORMAL_REGIONS = set(EXPECTED_PORTS)


def _template_config(path: Path) -> tuple[str, str, int, str]:
    content = path.read_text()
    name = re.search(r"^\s+- name: sharedApiClient-(\w+)\s*$", content, re.MULTILINE)
    region = re.search(r"^\s+SEKAI_REGION: (\w+)\s*$", content, re.MULTILINE)
    bind = re.search(r"--bind (\S+)", content)
    workers = re.findall(r"--workers (\d+)", content)

    assert name and region and bind
    assert workers == ["1"]
    host, port = bind.group(1).rsplit(":", 1)
    return name.group(1), region.group(1), int(port), host


def test_formal_shared_client_templates_match_region_contract():
    templates = sorted(EXAMPLES_DIR.glob("sharedApiClient*.yaml.example"))

    assert {
        path.name.removeprefix("sharedApiClient").removesuffix(".yaml.example").lower()
        for path in templates
    } == FORMAL_REGIONS
    assert len(templates) == len(FORMAL_REGIONS)

    for template in templates:
        name, region, port, host = _template_config(template)
        assert name == region
        assert region in FORMAL_REGIONS
        assert port == EXPECTED_PORTS[region]
        assert host == "127.0.0.1"


def test_cn_is_excluded_from_formal_shared_client_templates():
    assert not list(EXAMPLES_DIR.glob("sharedApiClientCN.yaml.example"))
    assert all(
        "cn" not in path.read_text().lower()
        for path in EXAMPLES_DIR.glob("sharedApiClient*.yaml.example")
    )


def test_tw_remote_canary_preserves_runtime_and_rollback_boundaries():
    template = CANARY_DIR / "sharedApiClientTW.yaml.example"
    content = template.read_text()

    assert "name: sharedApiClient-tw" in content
    assert "--workers 1" in content
    assert "--bind 127.0.0.1:39391" in content
    assert "SEKAI_REGION: tw" in content
    assert "SEKAI_ACCOUNT_PROVIDER: remote" in content
    assert "SEKAI_ACCOUNT_SERVICE_URL: ${SEKAI_ACCOUNT_SERVICE_URL}" in content
    assert "SEKAI_ACCOUNT_SERVICE_TOKEN: ${SEKAI_ACCOUNT_SERVICE_TOKEN}" in content
    assert "SEKAI_TW_ACCESS_TOKEN" not in content
    assert "SEKAI_TW_SDK_OPEN_ID" not in content

    local_template = (EXAMPLES_DIR / "sharedApiClientTW.yaml.example").read_text()
    assert "SEKAI_ACCOUNT_PROVIDER: remote" not in local_template
    assert "SEKAI_TW_ACCESS_TOKEN" in local_template

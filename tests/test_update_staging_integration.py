"""Phase 4.2 final staging acceptance evidence lane.

This file is the *last* staging-acceptance lane. It exercises the real production
write helpers against a temporary master/i18n working directory and a controlled
JSON-RPC stub, proving that every real candidate write lands in the **active
staging root** (never the formal working tree) until publication, and that a real
i18n generation failure leaves both formal trees byte-identical, the published
global ``version_info`` un-advanced, and both staging roots cleared.

Covered production functions (called for real, not mocked):
  1) ``save_info_from_suite_user()``  -> real banner + suite user info write into
     the active master staging root; formal master tree untouched until publish.
  2) ``refresh_information()``        -> real write into the active master staging
     root; formal tree untouched.
  3) ``_write_compact_master_alias_if_needed()`` + ``restore_compact_data()`` ->
     real compact alias write into master staging; formal tree untouched.
  4) A real ``update_i18n_files()`` / ``I18N_SPECIAL_HANDLERS`` path (``stamps``)
     writing into i18n staging, followed by a real i18n handler exception
     (malformed ``cards`` data) that drives the real ``_generate_and_publish``
     generation/publication boundary. The external master fetch is stubbed, but
     the i18n handler itself is NOT mocked. Master and i18n formal trees are
     asserted byte-identical via ``read_bytes`` snapshots (directory-relative
     paths), the global ``version_info`` is not advanced, and both staging roots
     are cleared.
  5) A success path: real ``_generate_and_publish`` generates + validates (via the
     real ``_validate_staged_json``) + publishes; every staged JSON is valid and
     every manifest path corresponds to a real write helper; the published global
     and formal trees advance only after success.

All network/RPC is stubbed; no production systems are touched. Snapshot helpers
read bytes and include directory-relative paths. No conditional assertions,
skips, or xfails.
"""

import os
import pathlib

import pytest

import check_update as cu
from utils.array_to_dict import restore_compact_data

# --------------------------------------------------------------------------- #
# Snapshot helpers: read bytes, include directory-relative paths
# --------------------------------------------------------------------------- #


def _snapshot_bytes(root: str) -> dict[str, bytes]:
    """Return {relative posix path: file bytes} for every file under ``root``.

    Uses ``pathlib.Path.read_bytes`` (never text) and stores directory-relative
    paths so two trees can be compared byte-for-byte by relative path.
    """
    out: dict[str, bytes] = {}
    base = pathlib.Path(root)
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = p.read_bytes()
    return out


# --------------------------------------------------------------------------- #
# JSON-RPC stub
# --------------------------------------------------------------------------- #


def _install_jsonrpc(monkeypatch, spec: dict):
    """Controlled JSON-RPC stub: ``spec`` maps method -> value or callable."""

    def _request(method, params=None):
        if method not in spec:
            raise AssertionError(f"unexpected JSON-RPC request: {method}")
        handler = spec[method]
        if callable(handler):
            return handler(params)
        return handler

    monkeypatch.setattr(cu.jsonrpc_client, "request", _request)


def _activate_manifest(monkeypatch, master_staging: str, i18n_staging: str):
    """Simulate an in-flight cycle by pointing the module globals at staging."""
    monkeypatch.setattr(cu, "_MASTER_STAGING_ROOT", master_staging)
    monkeypatch.setattr(cu, "_I18N_STAGING_ROOT", i18n_staging)
    monkeypatch.setattr(
        cu, "_STAGING_MANIFEST", {"master": [], "i18n": []}
    )


# =========================================================================== #
# 1) save_info_from_suite_user() writes banner + user info to master staging
# =========================================================================== #


def test_save_info_from_suite_user_writes_staging_only(monkeypatch, tmp_path):
    formal_master = str(tmp_path / "formal_master")
    staging_master = str(tmp_path / "staging_master")
    staging_i18n = str(tmp_path / "staging_i18n")
    os.makedirs(formal_master, exist_ok=True)
    os.makedirs(staging_i18n, exist_ok=True)

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", formal_master)
    monkeypatch.setattr(
        cu, "update_options",
        {"master": True, "i18n": True, "userInfo": True},
    )
    monkeypatch.setattr(cu, "pjsk_region", "jp")  # not en -> writes userInformations
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )
    _activate_manifest(monkeypatch, staging_master, staging_i18n)

    suite_user = {
        "userHomeBanners": [{"id": 1, "title": "Banner A"}],
        "userInformations": [{"id": 99, "title": "Info X"}],
    }
    _install_jsonrpc(monkeypatch, {"login_user_info": suite_user})

    before = _snapshot_bytes(formal_master)

    returned = cu.save_info_from_suite_user()

    # Real banner write landed in the active master staging root.
    banner_path = os.path.join(staging_master, "userHomeBanners.json")
    info_path = os.path.join(staging_master, "userInformations.json")
    assert os.path.exists(banner_path)
    assert os.path.exists(info_path)
    import json

    assert json.loads(
        pathlib.Path(banner_path).read_text(encoding="utf-8")
    ) == suite_user["userHomeBanners"]
    assert json.loads(
        pathlib.Path(info_path).read_text(encoding="utf-8")
    ) == suite_user["userInformations"]
    # Manifest recorded both real write-helper paths.
    assert "userHomeBanners.json" in cu._STAGING_MANIFEST["master"]
    assert "userInformations.json" in cu._STAGING_MANIFEST["master"]
    # The function returns the suite user (real return contract).
    assert returned == suite_user
    # The formal master working tree is byte-identical (untouched until publish).
    assert _snapshot_bytes(formal_master) == before


# =========================================================================== #
# 2) refresh_information() writes to master staging only
# =========================================================================== #


def test_refresh_information_writes_staging_only(monkeypatch, tmp_path):
    formal_master = str(tmp_path / "formal_master")
    staging_master = str(tmp_path / "staging_master")
    staging_i18n = str(tmp_path / "staging_i18n")
    os.makedirs(formal_master, exist_ok=True)
    os.makedirs(staging_i18n, exist_ok=True)

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", formal_master)
    monkeypatch.setattr(
        cu, "update_options",
        {"master": True, "i18n": True, "userInfo": True},
    )
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )
    _activate_manifest(monkeypatch, staging_master, staging_i18n)

    _install_jsonrpc(
        monkeypatch,
        {"fetch_information": {"informations": [{"id": 7, "title": "News"}]}},
    )

    before = _snapshot_bytes(formal_master)

    cu.refresh_information()

    info_path = os.path.join(staging_master, "userInformations.json")
    assert os.path.exists(info_path)
    import json

    assert json.loads(
        pathlib.Path(info_path).read_text(encoding="utf-8")
    ) == [{"id": 7, "title": "News"}]
    assert "userInformations.json" in cu._STAGING_MANIFEST["master"]
    # Formal master tree unchanged.
    assert _snapshot_bytes(formal_master) == before


# =========================================================================== #
# 3) _write_compact_master_alias_if_needed() + restore_compact_data()
# =========================================================================== #


def test_compact_master_alias_writes_staging_only(monkeypatch, tmp_path):
    formal_master = str(tmp_path / "formal_master")
    staging_master = str(tmp_path / "staging_master")
    staging_i18n = str(tmp_path / "staging_i18n")
    os.makedirs(formal_master, exist_ok=True)
    os.makedirs(staging_i18n, exist_ok=True)

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", formal_master)
    monkeypatch.setattr(
        cu, "update_options",
        {"master": True, "i18n": True, "userInfo": False},
    )
    monkeypatch.setattr(cu, "pjsk_region", "tw")  # cn/tw/kr triggers compact alias
    _activate_manifest(monkeypatch, staging_master, staging_i18n)

    compact = {"__ENUM__": {}, "id": [10, 20], "name": ["A", "B"]}
    expected = restore_compact_data(compact)

    before = _snapshot_bytes(formal_master)

    cu._write_compact_master_alias_if_needed("compactCards", compact)

    alias_path = os.path.join(staging_master, "cards.json")
    assert os.path.exists(alias_path)
    import json

    assert json.loads(
        pathlib.Path(alias_path).read_text(encoding="utf-8")
    ) == expected
    # restore_compact_data produced the documented list-of-dicts mapping.
    assert expected == [
        {"id": 10, "name": "A"},
        {"id": 20, "name": "B"},
    ]
    assert "cards.json" in cu._STAGING_MANIFEST["master"]
    # Formal master tree unchanged.
    assert _snapshot_bytes(formal_master) == before

    # No-op outside cn/tw/kr: re-point region and confirm nothing is written.
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    before_count = len(cu._STAGING_MANIFEST["master"])
    cu._write_compact_master_alias_if_needed("compactCards", compact)
    assert len(cu._STAGING_MANIFEST["master"]) == before_count


# =========================================================================== #
# 4) Real i18n special-handler write + real handler exception: generation
#    boundary discards staging, formal trees byte-identical, global unchanged
# =========================================================================== #


def test_real_i18n_handler_failure_keeps_trees_and_clears_staging(
    monkeypatch, tmp_path
):
    formal_master = str(tmp_path / "formal_master")
    formal_i18n = str(tmp_path / "formal_i18n")
    os.makedirs(formal_master, exist_ok=True)
    os.makedirs(formal_i18n, exist_ok=True)
    master_staging = formal_master + ".staging"
    i18n_staging = formal_i18n + ".staging"

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", formal_master)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", formal_i18n)
    monkeypatch.setattr(
        cu, "update_options",
        {"master": True, "i18n": True, "userInfo": False},
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    baseline = {"dataVersion": "OLD", "assetVersion": "OLD"}
    monkeypatch.setattr(cu, "version_info", dict(baseline))

    candidate = {
        "dataVersion": "NEW",
        "assetVersion": "NEW",
        "appVersion": "",
        "cdnVersion": 1,
    }
    # External master fetch is stubbed; the i18n handler itself is NOT mocked.
    # "stamps" is valid and writes i18n staging successfully; "cards" is malformed
    # (missing "prefix") and raises a real exception inside _update_i18n_cards.
    def _fetch_master_split(params):
        split = params[0] if params else None
        if split == "stamps":
            return {"stamps": [{"id": 1, "name": "StampA"}]}
        return {"cards": [{"id": 2}]}  # cards: missing "prefix" -> handler raises

    _install_jsonrpc(
        monkeypatch,
        {
            "is_login": True,
            "refresh_master_split_paths": {},
            "version_info": candidate,
            "master_split_paths": ["stamps", "cards"],
            "fetch_master_split": _fetch_master_split,
        },
    )

    master_before = _snapshot_bytes(formal_master)
    i18n_before = _snapshot_bytes(formal_i18n)

    # Observe the real i18n special-handler output reaching the i18n staging root
    # *during* generation (the handler itself is never mocked; we only record via
    # the real _write_i18n_json wrapper).
    i18n_written_to_staging: list[str] = []
    real_write_i18n_json = cu._write_i18n_json

    def _record_i18n_json(filename, payload):
        rel = os.path.join("ja", filename)
        i18n_written_to_staging.append(rel)
        assert os.path.join(i18n_staging, rel) == os.path.join(
            cu._staging_i18n_root(), "ja", filename
        )
        return real_write_i18n_json(filename, payload)

    monkeypatch.setattr(cu, "_write_i18n_json", _record_i18n_json)

    # The real generation/publication boundary raises on the i18n handler failure
    # (the malformed "cards" split lacks the "prefix" key the special handler
    # requires, so a KeyError propagates out of the real refresh_version path).
    with pytest.raises(KeyError):
        cu._generate_and_publish(daily=True)

    # 4a) A real i18n special-handler path (stamps) wrote into i18n staging during
    #     generation: the real handler wrote ja/stamp_name.json to the staging root.
    assert "ja/stamp_name.json" in i18n_written_to_staging
    # The formal master tree is byte-identical (no versions.json / no stamps.json).
    assert _snapshot_bytes(formal_master) == master_before
    # Formal i18n tree is byte-identical (no ja/stamp_name.json published).
    assert _snapshot_bytes(formal_i18n) == i18n_before
    # Global published version_info is NOT advanced past the candidate.
    assert cu.version_info == baseline
    # Both staging roots were cleared by the generation-failure boundary.
    assert not os.path.exists(master_staging)
    assert not os.path.exists(i18n_staging)


# =========================================================================== #
# 5) Success path: real _generate_and_publish validates every staged JSON via
#    the real _validate_staged_json, manifests map to real write helpers, and
#    only after success do the formal trees + global advance.
# =========================================================================== #


def test_success_path_validates_staged_json_and_publishes(monkeypatch, tmp_path):
    formal_master = str(tmp_path / "formal_master")
    formal_i18n = str(tmp_path / "formal_i18n")
    os.makedirs(formal_master, exist_ok=True)
    os.makedirs(formal_i18n, exist_ok=True)
    master_staging = formal_master + ".staging"
    i18n_staging = formal_i18n + ".staging"

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", formal_master)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", formal_i18n)
    monkeypatch.setattr(
        cu, "update_options",
        {"master": True, "i18n": True, "userInfo": False},
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    baseline = {"dataVersion": "OLD", "assetVersion": "OLD"}
    monkeypatch.setattr(cu, "version_info", dict(baseline))

    candidate = {
        "dataVersion": "NEW",
        "assetVersion": "NEW",
        "appVersion": "",
        "cdnVersion": 1,
    }
    # A single valid "stamps" split exercises the real I18N_SPECIAL_HANDLERS path.
    _install_jsonrpc(
        monkeypatch,
        {
            "is_login": True,
            "refresh_master_split_paths": {},
            "version_info": candidate,
            "master_split_paths": ["stamps"],
            "fetch_master_split": lambda p: {"stamps": [{"id": 1, "name": "StampA"}]},
        },
    )

    validated: list[str] = []
    from check_update import _validate_staged_json as _real_validate

    def _tracking_validate(file_path):
        validated.append(file_path)
        return _real_validate(file_path)  # real parse-only validation

    monkeypatch.setattr(cu, "_validate_staged_json", _tracking_validate)

    manifest = cu._generate_and_publish(daily=True)

    # The returned manifest describes exactly what was published.
    assert set(manifest.keys()) == {"master", "i18n"}
    assert "versions.json" in manifest["master"]
    assert "stamps.json" in manifest["master"]
    assert "ja/stamp_name.json" in manifest["i18n"]

    # Every manifest path was validated (parse-only) by the real helper: staged
    # JSON was all valid and each path corresponds to a real write helper.
    validated_rel = set()
    for fp in validated:
        if fp.startswith(master_staging):
            validated_rel.add(os.path.relpath(fp, master_staging))
        elif fp.startswith(i18n_staging):
            validated_rel.add(os.path.relpath(fp, i18n_staging))
    expected_rel = set(manifest["master"]) | set(manifest["i18n"])
    assert validated_rel == expected_rel

    # Only after a successful generation+publication does the global advance.
    assert cu.version_info == candidate
    # The formal master versions.json equals the candidate (real publish).
    import json

    assert json.loads(
        pathlib.Path(formal_master).joinpath("versions.json").read_text(
            encoding="utf-8"
        )
    ) == candidate
    # The formal i18n tree received the real special-handler output.
    assert pathlib.Path(formal_i18n).joinpath("ja", "stamp_name.json").exists()
    # Both staging roots are cleared on the success path too.
    assert not os.path.exists(master_staging)
    assert not os.path.exists(i18n_staging)

import json
import os
import sys
import uuid
from contextlib import contextmanager

import pytest

from utils import update_supervisor as supervisor


def _owner(tmp_path, *, locks=None):
    return supervisor.build_owner_metadata(
        run_id=uuid.uuid4().hex,
        pid=101,
        pgid=101,
        proc_starttime=123,
        task_kind=supervisor.TaskKind.ORDINARY,
        parent_pid=100,
        started_at=10.0,
        deadline=3610.0,
        lock_paths=locks or [str(tmp_path / "repo.lock")],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("run_id", uuid.uuid4().hex.upper()),
        ("pid", True),
        ("pgid", 102),
        ("started_at", 0.0),
        ("deadline", 10.0),
        ("lock_paths", ("/tmp/a.lock",)),
        ("lock_paths", ["relative.lock"]),
        ("lock_paths", ["/tmp/b.lock", "/tmp/a.lock"]),
        ("lock_paths", ["/tmp/a.lock", "/tmp/a.lock"]),
    ],
)
def test_disk_schema_rejects_noncanonical_values(tmp_path, field, value):
    payload = _owner(tmp_path).to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        supervisor.OwnerMetadata.from_dict(payload)


def test_build_normalizes_user_paths(tmp_path):
    lock = tmp_path / "repo.lock"
    owner = _owner(tmp_path, locks=[str(lock), str(tmp_path / "." / "repo.lock")])
    assert owner.lock_paths == (os.path.realpath(lock),)


def test_duplicate_json_keys_symlink_and_oversize_fail_closed(tmp_path):
    lock = tmp_path / "repo.lock"
    owner_path = supervisor.owner_metadata_path(str(lock))
    with open(owner_path, "w", encoding="utf-8") as file:
        file.write('{"schema_version":1,"schema_version":1}')
    assert supervisor.load_owner_metadata(str(lock)) is None
    os.unlink(owner_path)
    target = tmp_path / "target"
    target.write_text("{}")
    os.symlink(target, owner_path)
    assert supervisor.load_owner_metadata(str(lock)) is None
    os.unlink(owner_path)
    with open(owner_path, "wb") as file:
        file.write(b"x" * (64 * 1024 + 1))
    assert supervisor.load_owner_metadata(str(lock)) is None


def test_atomic_round_trip_permissions_and_full_match_delete(tmp_path):
    lock = tmp_path / "repo.lock"
    lock.touch()
    owner = _owner(tmp_path)
    supervisor.write_owner_metadata_for_locks(owner)
    owner_path = supervisor.owner_metadata_path(str(lock))
    assert os.stat(owner_path).st_mode & 0o777 == 0o600
    assert supervisor.load_owner_metadata(str(lock)) == owner
    changed = supervisor.OwnerMetadata.from_dict(
        {**owner.to_dict(), "deadline": owner.deadline + 1}
    )
    assert not supervisor.delete_owner_metadata_if_matched(str(lock), changed)
    assert supervisor.delete_owner_metadata_if_matched(str(lock), owner)
    assert lock.exists()


def test_multiwrite_failure_rolls_back_every_matching_owner(tmp_path, monkeypatch):
    locks = [tmp_path / "a.lock", tmp_path / "b.lock"]
    for lock in locks:
        lock.touch()
    owner = _owner(tmp_path, locks=[str(lock) for lock in locks])
    original = supervisor._atomic_write
    calls = 0

    def fail_after_replace(path, payload):
        nonlocal calls
        calls += 1
        original(path, payload)
        if calls == 2:
            raise OSError("directory fsync failed")

    monkeypatch.setattr(supervisor, "_atomic_write", fail_after_replace)
    with pytest.raises(OSError, match="directory fsync failed"):
        supervisor.write_owner_metadata_for_locks(owner)
    assert all(supervisor.load_owner_metadata(str(lock)) is None for lock in locks)
    assert all(lock.exists() for lock in locks)


def test_claim_cleanup_attempts_all_before_unlock(tmp_path, monkeypatch):
    locks = [tmp_path / "a.lock", tmp_path / "b.lock"]
    owner = _owner(tmp_path, locks=[str(lock) for lock in locks])
    events = []

    @contextmanager
    def fake_locks(paths, non_blocking):
        events.append(("locked", tuple(paths), non_blocking))
        try:
            yield
        finally:
            events.append(("unlocked",))

    def failing_delete(lock_path, metadata):
        events.append(("delete", lock_path))
        if lock_path == owner.lock_paths[0]:
            raise OSError("first delete failed")
        return True

    monkeypatch.setattr(supervisor, "repo_file_locks", fake_locks)
    monkeypatch.setattr(supervisor, "write_owner_metadata_for_locks", lambda _: None)
    monkeypatch.setattr(supervisor, "delete_owner_metadata_if_matched", failing_delete)
    with pytest.raises(supervisor.OwnerCleanupError):
        with supervisor.claimed_repo_locks(owner):
            events.append(("body",))
    assert [event[0] for event in events] == [
        "locked",
        "body",
        "delete",
        "delete",
        "unlocked",
    ]


def test_claim_cleanup_does_not_mask_body_exception(tmp_path, monkeypatch):
    owner = _owner(tmp_path)
    body_error = ValueError("body failed")

    @contextmanager
    def fake_locks(paths, non_blocking):
        yield

    monkeypatch.setattr(supervisor, "repo_file_locks", fake_locks)
    monkeypatch.setattr(supervisor, "write_owner_metadata_for_locks", lambda _: None)

    def failing_delete(lock_path, metadata):
        raise OSError(f"cleanup failed for {lock_path}")

    monkeypatch.setattr(supervisor, "delete_owner_metadata_if_matched", failing_delete)

    with pytest.raises(ValueError, match="body failed") as raised:
        with supervisor.claimed_repo_locks(owner):
            raise body_error

    assert raised.value is body_error
    assert len(raised.value.__notes__) == 1
    assert raised.value.__notes__[0].startswith(
        "owner cleanup had 1 errors while holding flocks:"
    )
    assert "cleanup failed" in raised.value.__notes__[0]


def test_proc_parser_and_verify_fail_closed(tmp_path, monkeypatch):
    line = "7 (a name (with parens)) S " + " ".join(
        [str(number) for number in range(1, 19)] + ["98765", "0"]
    )
    assert supervisor.parse_proc_stat_starttime(line) == 98765
    owner = _owner(tmp_path)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "process_exists", lambda _: True)
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(supervisor.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(supervisor, "read_proc_starttime", lambda _: 123)
    monkeypatch.setattr(supervisor, "read_proc_marker", lambda _: owner.run_id)
    assert supervisor.verify_owner(
        owner, list(owner.lock_paths), supervisor.TaskKind.ORDINARY
    ).verified
    assert not supervisor.verify_owner(
        owner, [str(tmp_path / "other.lock")], supervisor.TaskKind.ORDINARY
    ).verified


def test_non_linux_verification_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    result = supervisor.verify_owner(_owner(tmp_path), [str(tmp_path / "repo.lock")])
    assert not result.verified
    assert result.reason == "unsupported_platform"


def test_owner_json_is_valid_and_compact(tmp_path):
    owner = _owner(tmp_path)
    supervisor.write_owner_metadata_for_locks(owner)
    with open(
        supervisor.owner_metadata_path(owner.lock_paths[0]), encoding="utf-8"
    ) as file:
        payload = file.read()
    assert json.loads(payload) == owner.to_dict()
    assert "\n" not in payload
    if sys.platform != "linux":
        assert not supervisor.verify_owner(owner, list(owner.lock_paths)).verified

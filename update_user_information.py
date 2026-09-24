"""Periodically refresh in-game user information independently of versions."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from time import sleep

import ujson as json
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from git.repo import Repo
from git.util import Actor

from logging_config import configure_logging
from utils.constants import local_git_folder_names, pjsk_region, update_options
from utils.git import GitOutcome, GitResult, prepare_repo_for_update
from utils.git_lock import RepoLockUnavailable, repo_file_locks
from utils.git_publish import commit_diff, push_diff
from utils.jsonrpc_client import JSONRPCClient
from utils.user_information import (
    bootstrap_init_client,
    refresh_information,
    save_info_from_suite_user,
)

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
configure_logging(level=LOGLEVEL)
logger = logging.getLogger(__name__)

_MASTER_FILES = ("userHomeBanners.json", "userInformations.json")
_AUTHOR = Actor("user-information-bot", "anonymous@example.com")
masterdb_diff_folder_path = os.path.join(
    os.path.dirname(__file__), local_git_folder_names["masterDBDiff"]
)
jsonrpc_client = JSONRPCClient(f"http://localhost:{os.getenv('JSONRPC_PORT', '3939')}/")


def _write_master_file(relpath: str, data: object) -> None:
    if not update_options["master"]:
        return
    file_path = Path(masterdb_diff_folder_path) / relpath
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())


def _ahead_commits_owned_by_updater(repo: Repo) -> bool:
    """Return whether every ``origin/main..HEAD`` commit is an updater commit.

    Only non-merge commits authored by this updater and touching nothing but
    its own files qualify. Anything else (e.g. a retained master-version
    commit) belongs to check_update's journal workflow and must not be pushed
    from here.
    """
    commits = list(repo.iter_commits("origin/main..HEAD"))
    if not commits:
        return False
    for commit in commits:
        if commit.author.name != _AUTHOR.name or len(commit.parents) != 1:
            return False
        changes = commit.parents[0].diff(commit)
        paths = {
            path for diff in changes for path in (diff.a_path, diff.b_path) if path
        }
        if not paths.issubset(_MASTER_FILES):
            return False
    return True


def _push_retained_commits(repo: Repo) -> GitResult:
    """Push updater commits retained by an earlier failed push, then re-prepare.

    prepare runs with ``allow_push=False``, so a retained commit would otherwise
    block every later run (and check_update) with ``ahead_push_disabled``.
    """
    if not _ahead_commits_owned_by_updater(repo):
        raise RuntimeError(
            "master repository is not ready: ahead_push_disabled "
            "(ahead commits are not owned by the user information updater)"
        )
    logger.warning("[user_information] pushing retained user information commits")
    pushed = push_diff(repo, "push_retained_user_information")
    if pushed.outcome is not GitOutcome.OK:
        raise RuntimeError(f"retained user information push failed: {pushed.reason}")
    return prepare_repo_for_update(repo, allow_push=False)


def _prepare_master_repo() -> Repo:
    repo_path = Path(masterdb_diff_folder_path)
    repo = Repo(repo_path)
    result = prepare_repo_for_update(repo, allow_push=False)
    if result.reason == "ahead_push_disabled":
        result = _push_retained_commits(repo)
    if result.outcome is not GitOutcome.OK:
        raise RuntimeError(f"master repository is not ready: {result.reason}")
    return repo


def run_once() -> str:
    """Refresh user information once and push only the affected files."""
    bootstrap_init_client(jsonrpc_client, pjsk_region)
    jsonrpc_client.request("relogin")

    repo_path = Path(masterdb_diff_folder_path)
    try:
        with repo_file_locks([str(repo_path) + ".lock"], non_blocking=True):
            repo = _prepare_master_repo()
            save_info_from_suite_user(jsonrpc_client, pjsk_region, _write_master_file)
            if pjsk_region != "en":
                refresh_information(jsonrpc_client, _write_master_file)

            paths = [name for name in _MASTER_FILES if (repo_path / name).exists()]
            version = jsonrpc_client.request("version_info")
            commit = commit_diff(
                repo,
                operation="commit_user_information",
                folder_label=local_git_folder_names["masterDBDiff"],
                commit_message="update user information",
                author=_AUTHOR,
                paths=paths,
                version=version,
            )
            if commit.outcome is GitOutcome.NOTHING_TO_DO:
                logger.info("[user_information] no changes")
                return "nothing_to_do"
            if commit.outcome is not GitOutcome.OK:
                raise RuntimeError(f"user information commit failed: {commit.reason}")

            pushed = push_diff(repo, "push_user_information")
            if pushed.outcome is not GitOutcome.OK:
                raise RuntimeError(f"user information push failed: {pushed.reason}")
            logger.info("[user_information] updated and pushed")
            return "ok"
    except RepoLockUnavailable:
        logger.info("[user_information] skipped: master repository is locked")
        return "skipped:repo_lock"


def main() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(
        run_once,
        CronTrigger(
            minute=os.getenv("USER_INFORMATION_SCHEDULE_MINUTE", "*/30"),
            timezone="Asia/Tokyo",
        ),
        name="update_user_information",
        max_instances=1,
        coalesce=True,
    )
    try:
        initial_delay = float(os.getenv("USER_INFORMATION_START_DELAY", "0"))
        if initial_delay > 0:
            sleep(initial_delay)
        run_once()
    except Exception:
        logger.exception("[user_information] initial run failed")
    scheduler.start()


if __name__ == "__main__":
    main()

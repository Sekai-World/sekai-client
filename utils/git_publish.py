"""Stateless Git commit/push helpers shared by update processes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from git.repo import Repo
from git.util import Actor

from utils.git import GitOutcome, GitResult, push_current_head

logger = logging.getLogger(__name__)


def _safe_head_sha(repo: Repo) -> str | None:
    try:
        return repo.head.commit.hexsha
    except Exception:  # noqa: BLE001 - unborn repository
        return None


def commit_diff(
    repo: Repo | None,
    operation: str,
    folder_label: str,
    commit_message: str,
    author: Actor,
    paths: list[str] | None = None,
    version: dict[str, Any] | None = None,
) -> GitResult:
    """Stage and commit changes, without pushing."""
    if repo is None:
        logger.error("[%s] repository not initialized", operation)
        return GitResult(GitOutcome.FAILED, "repo_missing", operation)
    if version is None:
        logger.error("[%s] version info not available; cannot build commit", operation)
        return GitResult(GitOutcome.FAILED, "version_info_missing", operation)
    if not repo.is_dirty(untracked_files=True):
        return GitResult(GitOutcome.NOTHING_TO_DO, "clean", operation)
    if paths is not None and not paths:
        logger.debug(
            "[%s] explicit empty path list; no staged changes to commit", operation
        )
        return GitResult(GitOutcome.NOTHING_TO_DO, "no_staged_paths", operation)

    try:
        logger.debug("[%s] add files to staged in %s", operation, folder_label)
        repo.index.add(paths) if paths else repo.index.add("**")
        logger.debug("[%s] commit staged changes in %s", operation, folder_label)
        repo.index.commit(commit_message, author=author)
    except Exception as err:  # noqa: BLE001 - commit failure is structured
        logger.exception("[%s] failed to stage/commit", operation)
        return GitResult(GitOutcome.FAILED, "commit_failed", operation, detail=str(err))
    return GitResult(
        GitOutcome.OK, "committed", operation, local_sha=_safe_head_sha(repo)
    )


def push_diff(
    repo: Repo | None,
    operation: str,
    *,
    push_current_head_fn: Callable[..., GitResult] = push_current_head,
) -> GitResult:
    """Push the current HEAD and retain the existing pending-push semantics."""
    if repo is None:
        return GitResult(GitOutcome.FAILED, "repo_missing", operation)
    result = push_current_head_fn(repo, branch="main", require_remote_branch=True)
    if result.outcome is GitOutcome.PENDING_PUSH:
        logger.warning(
            "[%s] push pending (commit retained): reason=%s local_sha=%s",
            operation,
            result.reason,
            result.local_sha,
        )
    return result

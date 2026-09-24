"""Stateless Git commit/push helpers shared by update processes."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from git.repo import Repo
from git.util import Actor

from utils.git import GitOutcome, GitResult, push_current_head
from utils.redaction import REDACTED, redact_text

logger = logging.getLogger(__name__)

_MAX_DETAIL_LENGTH = 500
# Git errors echo the remote URL, which may embed credentials; drop it whole.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[oprsu]_|github_pat_)[A-Za-z0-9_]+|x-access-token(?:[:=][^\s@'\"]*)?",
    re.IGNORECASE,
)


def _redacted_detail(detail: str) -> str:
    """Return a single-line, credential-free, truncated push failure detail."""
    text = _URL_RE.sub(REDACTED, detail)
    text = _GITHUB_TOKEN_RE.sub(REDACTED, text)
    text = " ".join(redact_text(text).split())
    if len(text) > _MAX_DETAIL_LENGTH:
        text = text[:_MAX_DETAIL_LENGTH] + "..."
    return text


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
            "[%s] push pending (commit retained): reason=%s local_sha=%s detail=%s",
            operation,
            result.reason,
            result.local_sha,
            _redacted_detail(result.detail),
        )
    return result

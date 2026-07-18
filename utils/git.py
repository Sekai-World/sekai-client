from dataclasses import dataclass
from enum import Enum
from os import path

from git.exc import NoSuchPathError
from git.repo import Repo


class GitOutcome(Enum):
    """Converged outcome dimension for Phase 4.1 Git operations.

    - ``OK``: committed (if applicable) and pushed/verified; safe to proceed.
    - ``NOTHING_TO_DO``: no local change to commit (repository clean).
    - ``PENDING_PUSH``: committed locally but push failed or could not be
      verified; the local commit is *kept* and never reset/rebased/deleted.
    - ``BLOCKED``: a non-destructive precondition failed (detached, wrong
      branch, missing origin, dirty, diverged, expected-sha mismatch, or the
      remote ``<branch>`` does not exist); the repository is left untouched.
    - ``FAILED``: an unexpected/operational error (unborn HEAD, fetch failure,
      commit failure); distinct from a clean ``BLOCKED`` precondition.
    """

    OK = "ok"
    NOTHING_TO_DO = "nothing_to_do"
    PENDING_PUSH = "pending_push"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class GitResult:
    """Small structured result for Git operations.

    Attributes:
        outcome: one of :class:`GitOutcome`.
        reason: stable, machine-readable reason (no exception text).
        operation: name of the operation that produced this result.
        detail: optional free-form detail (e.g. exception message).
        local_sha: hex SHA of the local HEAD/commit when known.
        remote_sha: hex SHA of the remote ref when known (after fetch/push).
    """

    outcome: GitOutcome
    reason: str = ""
    operation: str = ""
    detail: str = ""
    local_sha: str | None = None
    remote_sha: str | None = None

    def __bool__(self) -> bool:
        # Truthy only when the work was fully committed AND pushed/verified.
        # This preserves the historical ``if commit_master_diff():`` contract
        # used by the production call sites. Every other outcome (including a
        # pending push and a commit failure) is falsy.
        return self.outcome is GitOutcome.OK


# Error flags that mean the push must not proceed / was rejected. We never
# force-push, so any of these is a real failure.
def _push_rejected(
    reason: str, operation: str, local_sha: str | None, detail: str
) -> GitResult:
    return GitResult(
        outcome=GitOutcome.PENDING_PUSH,
        reason=reason,
        operation=operation,
        local_sha=local_sha,
        detail=detail,
    )


def check_git_folder(folder_path: str, remote_git_url_base: str):
    try:
        return Repo(folder_path)
    except NoSuchPathError:
        return Repo.clone_from(
            f"{remote_git_url_base}/{path.basename(folder_path)}",
            folder_path,
            branch="main",
        )


def push_current_head(
    repo: Repo,
    remote_name: str = "origin",
    branch: str = "main",
    expected_sha: str | None = None,
    require_remote_branch: bool = True,
) -> GitResult:
    """Push the current HEAD to ``refs/heads/<branch>`` on the remote.

    Non-destructive by construction: explicit ``HEAD:refs/heads/<branch>``
    refspec, never forces, never resets/rebases/deletes/reclones. On any
    failure or verification gap it returns ``PENDING_PUSH`` while keeping the
    local HEAD, index, and working tree intact.

    When ``require_remote_branch`` is ``True`` (the default, enforced on the
    production path) the remote ``<branch>`` must already exist; if it does not,
    the push is refused with ``BLOCKED`` / ``missing_remote_branch`` rather than
    auto-creating the branch.

    Steps:
        1. Capture the pre-push local SHA (an unborn HEAD is a ``FAILED``, never
           pushed).
        2. If ``expected_sha`` is given and differs, do **not** push; return
           ``BLOCKED`` (the remote is left unchanged).
        3. If ``require_remote_branch`` and the remote branch is absent, return
           ``BLOCKED`` (no branch is created).
        4. Push with the explicit refspec; reject empty results; explicitly
           scan per-ref error flags and call ``PushInfoList.raise_if_error``;
           verify the targeted ref.
        5. Fetch (prune) and confirm ``origin/<branch>`` equals the pre-push SHA.
           If it cannot be confirmed, return ``PENDING_PUSH``.
    """
    operation = f"push HEAD:refs/heads/{branch}"
    remote_ref = f"refs/heads/{branch}"
    remote_ref_name = f"{remote_name}/{branch}"

    try:
        local_sha = repo.head.commit.hexsha
    except Exception as err:  # noqa: BLE001 - unborn HEAD is a structured failure
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="unborn",
            operation=operation,
            detail=f"cannot push an unborn HEAD: {err}",
        )

    if expected_sha is not None and local_sha != expected_sha:
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="expected_sha_mismatch",
            operation=operation,
            local_sha=local_sha,
            detail=f"local {local_sha} != expected {expected_sha}",
        )

    if require_remote_branch:
        # Refresh remote refs (prune) before deciding whether <branch> exists,
        # so a stale local origin/main never makes us (re)create a deleted
        # remote branch. A fetch failure is surfaced per the existing contract.
        try:
            repo.remote(remote_name).fetch(prune=True)
        except Exception as err:  # noqa: BLE001
            return _push_rejected(
                "fetch_failed", operation, local_sha, str(err)
            )
        if remote_ref_name not in [ref.name for ref in repo.remote(remote_name).refs]:
            return GitResult(
                outcome=GitOutcome.BLOCKED,
                reason="missing_remote_branch",
                operation=operation,
                local_sha=local_sha,
            )

    return _do_push_and_verify(
        repo, remote_name, branch, remote_ref, remote_ref_name, operation, local_sha
    )


def _do_push_and_verify(
    repo: Repo,
    remote_name: str,
    branch: str,
    remote_ref: str,
    remote_ref_name: str,
    operation: str,
    local_sha: str | None,
) -> GitResult:
    """Push ``HEAD:remote_ref`` and verify the remote matches the local SHA."""
    refspec = f"HEAD:{remote_ref}"

    try:
        push_results = repo.remote(remote_name).push(refspec=refspec)
        if not push_results:
            return _push_rejected(
                "empty_push_result", operation, local_sha, "push returned no results"
            )
        # Explicit per-ref error-flag scan (stable PENDING_PUSH / push_rejected).
        for info in push_results:
            if info.remote_ref_string != remote_ref:
                return _push_rejected(
                    "unexpected_ref",
                    operation,
                    local_sha,
                    f"pushed to {info.remote_ref_string}, expected {remote_ref}",
                )
            if (
                info.flags
                & (
                    info.REJECTED
                    | info.REMOTE_REJECTED
                    | info.REMOTE_FAILURE
                    | info.ERROR
                    | info.NO_MATCH
                )
            ):
                return _push_rejected(
                    "push_rejected",
                    operation,
                    local_sha,
                    f"flags={info.flags} summary={getattr(info, 'summary', '')}",
                )
        # Backstop: raise on any other error reported by gitpython.
        push_results.raise_if_error()
    except Exception as err:  # noqa: BLE001 - keep local commit, surface pending
        return _push_rejected("push_rejected", operation, local_sha, str(err))

    # Post-push verification: the remote ref must equal the pre-push SHA.
    try:
        repo.remote(remote_name).fetch(prune=True)
        confirmed_sha = repo.commit(remote_ref_name).hexsha
    except Exception as err:  # noqa: BLE001
        return _push_rejected("verify_fetch_failed", operation, local_sha, str(err))
    if confirmed_sha != local_sha:
        return GitResult(
            outcome=GitOutcome.PENDING_PUSH,
            reason="verify_sha_mismatch",
            operation=operation,
            local_sha=local_sha,
            remote_sha=confirmed_sha,
        )

    return GitResult(
        outcome=GitOutcome.OK,
        operation=operation,
        local_sha=local_sha,
        remote_sha=confirmed_sha,
    )


def _safe_local_sha(repo: Repo) -> str | None:
    """Return the local HEAD SHA, or ``None`` for an unborn/empty repository."""
    try:
        return repo.head.commit.hexsha
    except Exception:  # noqa: BLE001 - unborn branch has no commit yet
        return None


def _remote_main_sha(repo: Repo, remote_name: str, branch: str) -> str | None:
    """Return the remote-tracking SHA for ``<remote>/<branch>``, or ``None``."""
    try:
        return repo.commit(f"{remote_name}/{branch}").hexsha
    except Exception:  # noqa: BLE001 - ref not present after fetch
        return None


def _relation(repo: Repo, local_commit, remote_commit) -> str:
    """Classify local vs remote using a real shared merge-base."""
    local_sha = local_commit.hexsha
    remote_sha = remote_commit.hexsha
    if local_sha == remote_sha:
        return "equal"
    merge_bases = repo.merge_base(local_commit, remote_commit)
    common = merge_bases[0] if merge_bases else None
    if common is not None and common.hexsha == local_sha:
        return "behind"  # local is an ancestor of remote
    if common is not None and common.hexsha == remote_sha:
        return "ahead"  # remote is an ancestor of local
    return "diverged"


def _structural_blocked(
    repo: Repo, remote_name: str, branch: str, operation: str
) -> GitResult | None:
    """Return a BLOCKED result for precondition failures, or ``None``.

    Never mutates the repository (does not create branches or remotes).
    """
    if repo.head.is_detached:
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="detached_head",
            operation=operation,
            local_sha=_safe_local_sha(repo),
        )
    try:
        current_branch = repo.active_branch.name
    except Exception:
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="unborn_or_detached",
            operation=operation,
        )
    if current_branch != branch:
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="wrong_branch",
            operation=operation,
            local_sha=_safe_local_sha(repo),
        )
    if remote_name not in [r.name for r in repo.remotes]:
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="missing_origin",
            operation=operation,
            local_sha=_safe_local_sha(repo),
        )
    if repo.is_dirty(untracked_files=True):
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="dirty",
            operation=operation,
            local_sha=_safe_local_sha(repo),
        )
    return None


def _fetch_or_fail(
    repo: Repo, remote_name: str
) -> GitResult | None:
    """Fetch remote refs (prune), returning a FAILED result on error, else ``None``."""
    try:
        repo.remote(remote_name).fetch(prune=True)
    except Exception as err:  # noqa: BLE001
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="fetch_failed",
            operation="fetch",
            local_sha=_safe_local_sha(repo),
            detail=str(err),
        )
    return None


def _try_ff_only(
    repo: Repo,
    remote_ref: str,
    local_sha: str | None,
    remote_sha: str,
    operation: str,
) -> GitResult:
    """Fast-forward the local branch onto the remote ref (non-destructive).

    A fast-forward failure is an operational error (the local state could not
    be advanced) and is reported as ``FAILED`` with ``operation="fast_forward"``;
    the local HEAD is left unchanged.
    """
    try:
        repo.git.merge("--ff-only", remote_ref)
    except Exception as err:  # noqa: BLE001
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="ff_failed",
            operation="fast_forward",
            local_sha=local_sha,
            remote_sha=remote_sha,
            detail=str(err),
        )
    return GitResult(
        outcome=GitOutcome.OK,
        reason="fast_forwarded",
        operation=operation,
        local_sha=repo.head.commit.hexsha,
        remote_sha=remote_sha,
    )


def _handle_ahead(
    repo: Repo,
    remote_name: str,
    branch: str,
    expected_sha: str | None,
    allow_push: bool,
    require_remote_branch: bool,
    operation: str,
    local_sha: str | None,
) -> GitResult:
    """A local-ahead repository pushes the *same* HEAD; push verifies it."""
    if not allow_push:
        return GitResult(
            outcome=GitOutcome.PENDING_PUSH,
            reason="ahead_push_disabled",
            operation=operation,
            local_sha=local_sha,
        )
    push_result = push_current_head(
        repo,
        remote_name=remote_name,
        branch=branch,
        expected_sha=expected_sha,
        require_remote_branch=require_remote_branch,
    )
    if push_result.outcome is GitOutcome.OK:
        push_result.reason = "ahead_pushed"
    return push_result


def prepare_repo_for_update(
    repo: Repo,
    remote_name: str = "origin",
    branch: str = "main",
    expected_sha: str | None = None,
    allow_push: bool = True,
    require_remote_branch: bool = True,
) -> GitResult:
    """Prepare a repository for an update cycle without any destructive pull.

    Explicit structural/state checks, then a fetch (prune), then a real
    shared-base relation (equal / behind / ahead / diverged):

    - detached HEAD / unborn-or-detached / wrong branch / missing origin /
      dirty -> ``BLOCKED`` (no branch/remote is ever created).
    - fetch failure -> ``FAILED`` (operation=fetch).
    - the remote ``<branch>`` does not exist (missing/deleted/stale tracking
      ref) -> ``BLOCKED`` (reason=missing_remote_branch); ``main`` is never
      auto-created by prepare or by the underlying push.
    - local unborn + remote missing ``<branch>`` -> ``BLOCKED`` (both missing).
    - local unborn + remote ``<branch>`` exists -> fast-forward only (``OK``).
    - equal -> ``OK``; behind -> fast-forward only (``OK``).
    - ahead (remote branch exists) -> push the same local HEAD and verify
      (``OK`` / ``PENDING_PUSH``).
    - diverged -> ``BLOCKED``.

    This never calls ``pull()`` and never mutates the repository when blocked.
    """
    operation = "prepare_repo_for_update"

    blocked = _structural_blocked(repo, remote_name, branch, operation)
    if blocked is not None:
        return blocked

    fetch_err = _fetch_or_fail(repo, remote_name)
    if fetch_err is not None:
        return fetch_err

    local_sha = _safe_local_sha(repo)
    remote_ref = f"{remote_name}/{branch}"
    remote_sha = _remote_main_sha(repo, remote_name, branch)

    # Local unborn branch.
    if local_sha is None:
        if remote_sha is None:
            return GitResult(
                outcome=GitOutcome.BLOCKED,
                reason="unborn_no_remote",
                operation=operation,
            )
        return _try_ff_only(repo, remote_ref, None, remote_sha, operation)

    # Local has a commit but the remote branch does not exist.
    if remote_sha is None:
        if require_remote_branch:
            return GitResult(
                outcome=GitOutcome.BLOCKED,
                reason="missing_remote_branch",
                operation=operation,
                local_sha=local_sha,
            )
        return _handle_ahead(
            repo,
            remote_name,
            branch,
            expected_sha,
            allow_push,
            require_remote_branch,
            operation,
            local_sha,
        )

    relation = _relation(repo, repo.head.commit, repo.commit(remote_ref))

    if relation == "equal":
        return GitResult(
            outcome=GitOutcome.OK,
            reason="equal",
            operation=operation,
            local_sha=local_sha,
            remote_sha=remote_sha,
        )
    if relation == "behind":
        return _try_ff_only(repo, remote_ref, local_sha, remote_sha, operation)
    if relation == "diverged":
        return GitResult(
            outcome=GitOutcome.BLOCKED,
            reason="diverged",
            operation=operation,
            local_sha=local_sha,
            remote_sha=remote_sha,
        )
    # relation == "ahead"
    return _handle_ahead(
        repo,
        remote_name,
        branch,
        expected_sha,
        allow_push,
        require_remote_branch,
        operation,
        local_sha,
    )

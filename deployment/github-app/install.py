"""Install repository-scoped GitHub App authentication for update repositories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from stat import S_IMODE

REPOSITORIES = (
    "sekai-i18n",
    "sekai-master-db-cn-diff",
    "sekai-master-db-diff",
    "sekai-master-db-en-diff",
    "sekai-master-db-kr-diff",
    "sekai-master-db-tc-diff",
)


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def install(
    project_root: Path,
    config_directory: Path,
    private_key_source: Path,
    app_id: int,
    installation_id: int,
) -> None:
    repositories = [project_root / name for name in REPOSITORIES]
    missing = [
        repository.name for repository in repositories if not repository.is_dir()
    ]
    if missing:
        raise RuntimeError("missing update repositories: " + ", ".join(missing))
    helper = project_root / "utils" / "github_app_credentials.py"
    python = project_root / ".venv" / "bin" / "python"
    if not helper.is_file() or not python.is_file() or not private_key_source.is_file():
        raise RuntimeError(
            "project helper, virtual environment, or private key is missing"
        )

    try:
        metadata = config_directory.stat()
    except FileNotFoundError:
        config_directory.mkdir(mode=0o700, parents=True)
        metadata = config_directory.stat()
    if metadata.st_uid != os.geteuid() or S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("config directory must be private and owner-controlled")
    private_key = config_directory / "private-key.pem"
    shutil.copyfile(private_key_source, private_key)
    private_key.chmod(0o600)
    config = config_directory / "config.json"
    _atomic_json(
        config,
        {
            "app_id": app_id,
            "installation_id": installation_id,
            "private_key_path": str(private_key),
            "repositories": [f"Sekai-World/{name}" for name in REPOSITORIES],
        },
    )

    helper_command = f"!{python} {helper}"
    for repository in repositories:
        _run_git(
            repository,
            "remote",
            "set-url",
            "origin",
            f"https://github.com/Sekai-World/{repository.name}.git",
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "--local",
                "--unset-all",
                "credential.helper",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _run_git(repository, "config", "--local", "--add", "credential.helper", "")
        _run_git(
            repository,
            "config",
            "--local",
            "--add",
            "credential.helper",
            helper_command,
        )
        _run_git(repository, "config", "--local", "credential.useHttpPath", "true")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path("/root/sekai-client"))
    parser.add_argument(
        "--config-directory",
        type=Path,
        default=Path("/root/.config/sekai-github-app"),
    )
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--app-id", type=int, default=4325474)
    parser.add_argument("--installation-id", type=int, default=147244811)
    arguments = parser.parse_args()
    install(
        arguments.project_root.resolve(),
        arguments.config_directory.resolve(),
        arguments.private_key.resolve(),
        arguments.app_id,
        arguments.installation_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

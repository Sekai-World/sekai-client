from os import path

from git.exc import NoSuchPathError
from git.repo import Repo


def check_git_folder(folder_path: str, remote_git_url_base: str):
    try:
        return Repo(folder_path)
    except NoSuchPathError:
        return Repo.clone_from(
            f"{remote_git_url_base}/{path.basename(folder_path)}",
            folder_path,
            branch="main",
        )

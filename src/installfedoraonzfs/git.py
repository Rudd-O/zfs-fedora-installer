"""Git-related utilities."""

import logging
import os
import shlex
import shutil
import tempfile

from installfedoraonzfs.cmd import check_call, check_output, get_distro_release_info
from pathlib import Path
from typing import Protocol


_LOGGER = logging.getLogger("git")


class Gitter(Protocol):
    """Protocol for a class that can check out a repo."""

    def checkout_repo_at(
        self, repo: str, project_dir: Path, branch: str, update: bool = True
    ) -> None:
        """Check out a repository URL to `project_dir`."""
        ...


class NetworkedGitter:
    """A Gitter that requires use of the network."""

    def checkout_repo_at(
        self, repo: str, project_dir: Path, branch: str, update: bool = True
    ) -> None:
        """Check out a repository URL to `project_dir`."""
        qbranch = shlex.quote(branch)
        if os.path.isdir(project_dir):
            if update:
                _LOGGER.info("Updating and checking out git repository: %s", repo)
                check_call("git fetch".split(), cwd=project_dir)
                check_call(
                    [
                        "bash",
                        "-c",
                        f"git reset --hard origin/{qbranch}"
                        f" || git reset --hard {qbranch}",
                    ],
                    cwd=project_dir,
                )
        else:
            _LOGGER.info("Cloning git repository: %s", repo)
            check_call(["git", "clone", repo, str(project_dir)])
            check_call(
                [
                    "bash",
                    "-c",
                    f"git reset --hard origin/{qbranch} || git reset --hard {qbranch}",
                ],
                cwd=project_dir,
            )
        check_call(["git", "--no-pager", "show"], cwd=project_dir)


class QubesGitter:
    """A Gitter that requires use of the network."""

    def __init__(self, dispvm_template: str):
        """Initialize the gitter.

        Args:
          dispvm_template: mandatory name of disposable VM to use.
        """
        self.dispvm_template = dispvm_template

    def checkout_repo_at(
        self, repo: str, project_dir: Path, branch: str, update: bool = True
    ) -> None:
        """Check out a repository URL to `project_dir`."""
        from installfedoraonzfs.pm import LocalQubesOSPackageManager

        local_pkgmgr = LocalQubesOSPackageManager()
        local_pkgmgr.ensure_packages_installed(["git-core"])

        qbranch = shlex.quote(branch)
        abs_project_dir = os.path.abspath(project_dir)
        if os.path.isdir(project_dir) and update:
            _LOGGER.info("Update requested — removing existing repo: %s", project_dir)
            shutil.rmtree(project_dir)
        if not os.path.isdir(project_dir):
            _LOGGER.info("Cloning git repository: %s", repo)
            with tempfile.TemporaryDirectory() as tempdir:
                installgit = "(which git || dnf install -y git-core)"
                gitclone = shlex.join(
                    ["git", "clone", "--", repo, os.path.basename(tempdir)]
                )
                tar = f"cd {shlex.quote(os.path.basename(tempdir))} && tar c ."
                gitcloneandtar = f"{installgit} >&2 && {gitclone} >&2 && {tar}"
                # default_dvm = check_output(["qubes-prefs", "default_dispvm"])
                indvm = shlex.join(
                    [
                        "qvm-run",
                        "-a",
                        "-p",
                        "--no-filter-escape-chars",
                        "--no-color-output",
                        f"--dispvm={self.dispvm_template}",
                        "bash",
                        "-c",
                        gitcloneandtar,
                    ]
                )
                extract = "tar x"
                chdirandextract = (
                    "("
                    f"cd {shlex.quote(tempdir)} && mkdir -p incoming"
                    f" && mkdir -p {shlex.quote(os.path.dirname(abs_project_dir))}"
                    f" && cd incoming && {extract}"
                    f" && cd .."
                    f" && mv incoming {shlex.quote(abs_project_dir)}"
                    ")"
                )
                fullcommand = [
                    "bash",
                    "-c",
                    f"set -o pipefail ; {indvm} | {chdirandextract}",
                ]
                check_call(fullcommand)
            check_call(
                [
                    "bash",
                    "-c",
                    f"git reset --hard origin/{qbranch} || git reset --hard {qbranch}",
                ],
                cwd=project_dir,
            )
        check_call(["git", "--no-pager", "show"], cwd=project_dir)


def gitter_factory(dispvm_template: str | None = None) -> Gitter:
    """Return a Gitter that is compatible with the system."""
    info = get_distro_release_info()
    if info.get("ID") == "qubes":
        if not dispvm_template:
            dispvm_template = check_output(["qubes-prefs", "default_dispvm"]).rstrip()
            if not dispvm_template:
                raise ValueError(
                    "there is no default disposable qube template on this system;"
                    " you must specify a disposable qube template with --dispvm-template"
                    " when using this program on this system"
                )
        return QubesGitter(dispvm_template)
    if dispvm_template:
        raise ValueError(
            "disposable qube may not be specified when using this"
            f" program on a {info.get('NAME')} system"
        )
    return NetworkedGitter()

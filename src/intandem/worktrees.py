"""Git worktree management for running parallel implementers on one repository.

Each implementer gets its own branch ``intandem/<name>`` checked out under the
state root so the main working tree stays untouched. Git's worktree list is
used as the source of truth rather than maintaining a separate registry.

Implementers edit files without committing; landing commits their remaining
changes and either merges the branch into the current branch or removes it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "intandem/"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Build/test caches that should never be committed when landing, even if unignored.
GENERATED = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "*.pyc")
_LANDED = (
    ".",
    *(f":(exclude,glob)**/{pattern}" for pattern in GENERATED),
    *(f":(exclude,glob)**/{pattern}/**" for pattern in GENERATED if "*" not in pattern),
)
"""Git pathspec matching all implementer changes except ``GENERATED``."""


class GitError(Exception):
    """Raised when a git subprocess fails."""


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=120
    )
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise GitError(f"git {args[0]}: {' '.join(detail[-3:]) or f'exit {done.returncode}'}")
    return done.stdout


def repo_root(path: Path) -> Path:
    """Return the main working tree directory for the git repository containing ``path``."""
    try:
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except GitError as exc:
        raise GitError(f"{path} is not inside a git repository") from exc
    root = Path(common).parent
    if not (root / ".git").exists():
        raise GitError(f"{path} belongs to a bare repository; implementers need a working tree")
    return root


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise GitError(
            f"bad implementer name {name!r}: lower-case letters, digits and dashes, "
            "starting with a letter or digit, at most 40 characters"
        )
    return name


@dataclass(frozen=True, slots=True)
class Worktree:
    name: str
    path: Path
    branch: str


def worktree_home(state_root: Path, repo: Path) -> Path:
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:6]
    return state_root.expanduser() / "worktrees" / f"{repo.name}-{digest}"


def list_worktrees(repo: Path) -> list[Worktree]:
    """List inTandem implementer worktrees registered in ``repo``."""
    found: list[Worktree] = []
    path: Path | None = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/" + BRANCH_PREFIX) and path is not None:
            branch = line.removeprefix("branch refs/heads/")
            found.append(Worktree(branch.removeprefix(BRANCH_PREFIX), path, branch))
    return found


def find_worktree(repo: Path, name: str) -> Worktree:
    for worktree in list_worktrees(repo):
        if worktree.name == name:
            return worktree
    known = ", ".join(w.name for w in list_worktrees(repo)) or "none"
    raise GitError(f"no implementer named {name!r} in {repo} (running: {known})")


def create_worktree(repo: Path, state_root: Path, name: str, base: str | None) -> Worktree:
    """Create a branch and worktree for ``name``, or return the existing one."""
    check_name(name)
    for worktree in list_worktrees(repo):
        if worktree.name == name:
            return worktree
    branch = BRANCH_PREFIX + name
    if git(repo, "branch", "--list", branch).strip():
        raise GitError(
            f"branch {branch} exists without a worktree; land or delete it, or pick another name"
        )
    path = worktree_home(state_root, repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base or "HEAD")
    return Worktree(name, path, branch)


def commit_all(worktree: Worktree, message: str) -> bool:
    """Commit all uncommitted changes in ``worktree``. Returns ``False`` if clean."""
    git(worktree.path, "add", "-A", "--", *_LANDED)
    if not git(worktree.path, "diff", "--cached", "--name-only").strip():
        return False
    git(
        worktree.path,
        "-c",
        "user.name=inTandem",
        "-c",
        "user.email=intandem@localhost",
        "commit",
        "--no-verify",
        "-m",
        message,
    )
    return True


def review(repo: Path, worktree: Worktree, max_chars: int) -> dict[str, object]:
    """Return diff stats and a truncated patch of all changes since the branch fork point."""
    head = git(repo, "rev-parse", "HEAD").strip()
    base = git(repo, "merge-base", head, worktree.branch).strip()
    # Mark untracked files with intent-to-add so git diff includes them without staging content.
    git(worktree.path, "add", "--intent-to-add", "--all", "--", *_LANDED)
    stat = git(worktree.path, "diff", "--stat", base, "--", *_LANDED).strip()
    diff = git(worktree.path, "diff", base, "--", *_LANDED)
    truncated = len(diff) > max_chars
    return {
        "branch": worktree.branch,
        "worktree": str(worktree.path),
        "base": base,
        "repo_head_moved": base != head,
        "stat": stat or "(no changes)",
        "diff": diff[:max_chars] + ("\n... (truncated)" if truncated else ""),
        "truncated": truncated,
    }


def merge(repo: Path, worktree: Worktree, message: str) -> str:
    """Merge ``worktree.branch`` into ``repo``'s current branch, aborting cleanly on conflict."""
    current = git(repo, "branch", "--show-current").strip()
    if not current:
        raise GitError(f"{repo} is on a detached HEAD; check out the branch to merge into")
    done = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "-m", message, worktree.branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if done.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        detail = " ".join((done.stdout + done.stderr).strip().splitlines()[-4:])
        raise GitError(f"merge into {current} failed and was aborted: {detail}")
    return current


def remove(repo: Path, worktree: Worktree) -> None:
    """Remove the worktree directory and delete its branch."""
    git(repo, "worktree", "remove", "--force", str(worktree.path))
    git(repo, "branch", "-D", worktree.branch)


def worktree_for_dir(path: Path) -> tuple[Path, Worktree] | None:
    """Return ``(main_repo, Worktree)`` if ``path`` is a linked git worktree, else ``None``.

    Works for both ``spawn_implementer`` worktrees (``intandem/<name>``) and
    manually created linked worktrees on any branch. Returns ``None`` when
    ``path`` is the main working tree itself or not inside a git repository.
    """
    resolved = path.expanduser().resolve()
    try:
        repo = repo_root(resolved)
    except GitError:
        return None
    if resolved == repo.resolve():
        return None
    branch = git(resolved, "branch", "--show-current", check=False).strip()
    if not branch:
        return None
    name = branch.removeprefix(BRANCH_PREFIX) if branch.startswith(BRANCH_PREFIX) else resolved.name
    return repo, Worktree(name=name, path=resolved, branch=branch)


def target_branch(repo: Path) -> str | None:
    """Return the checked-out branch in ``repo``, or ``None`` if HEAD is detached."""
    return git(repo, "branch", "--show-current", check=False).strip() or None


def merge_base(repo: Path, worktree: Worktree) -> tuple[str | None, bool]:
    """Return ``(base_sha, repo_head_moved)`` between ``repo`` HEAD and ``worktree.branch``."""
    try:
        head = git(repo, "rev-parse", "HEAD").strip()
        base = git(repo, "merge-base", head, worktree.branch).strip()
    except GitError:
        return None, False
    return base, base != head


def archive_state(state_root: Path, task_dir: Path) -> Path | None:
    """Move a finished implementer's state directory under ``landed/``."""
    from intandem.config import TaskPaths

    root = TaskPaths.for_task(state_root, task_dir).root
    if not root.exists():
        return None
    archive = state_root.expanduser() / "landed" / f"{root.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.rename(archive)

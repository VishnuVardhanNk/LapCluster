"""Getting a repository onto this laptop and choosing which files to review."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
    "node_modules", "__pycache__", "dist", "build", "target", "vendor",
}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".sql",
}
# A file plus the review prompt has to fit in the model's 8192-token context.
MAX_FILE_BYTES = 20_000
URL_PREFIXES = ("http://", "https://", "git@", "ssh://", "file://")


class RepoError(Exception):
    pass


@dataclass
class SourceFile:
    path: str
    content: str


@dataclass
class Collected:
    files: list[SourceFile] = field(default_factory=list)
    # (path, reason) for source files that could not be sent for review
    skipped: list[tuple[str, str]] = field(default_factory=list)


def collect_files(root: Path | str) -> Collected:
    root = Path(root)
    collected = Collected()
    for folder, subfolders, names in os.walk(root):
        subfolders[:] = sorted(d for d in subfolders if d not in SKIP_DIRS)
        for name in sorted(names):
            full = Path(folder) / name
            if full.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            relative = full.relative_to(root).as_posix()
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    collected.skipped.append(
                        (relative, f"larger than {MAX_FILE_BYTES // 1000} KB")
                    )
                    continue
                content = full.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                collected.skipped.append((relative, "not UTF-8 text"))
                continue
            except OSError:
                # For example a broken symlink or a file we may not open.
                collected.skipped.append((relative, "could not be read"))
                continue
            if content.strip():
                collected.files.append(SourceFile(relative, content))
    collected.files.sort(key=lambda f: f.path)
    return collected


def _make_writable_and_retry(function, path, _error):
    # Git marks its object files read-only, which stops Windows deleting them.
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove_tree(path: str) -> None:
    handler = "onexc" if sys.version_info >= (3, 12) else "onerror"
    shutil.rmtree(path, **{handler: _make_writable_and_retry})


@contextmanager
def open_repo(source: str) -> Iterator[Path]:
    """Yield a local folder for `source`, which is a folder path or a git URL.

    A URL is cloned into a temporary folder that is deleted afterwards.
    """
    if not source.startswith(URL_PREFIXES):
        folder = Path(source)
        if not folder.is_dir():
            raise RepoError(f"Not a folder: {source}")
        yield folder
        return

    clone_dir = tempfile.mkdtemp(prefix="lapclusters-")
    try:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", "--", source, clone_dir],
                check=True,
                capture_output=True,
                text=True,
                # Fail at once on a private repository instead of waiting for a
                # username nobody can type.
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except FileNotFoundError as exc:
            raise RepoError("Could not clone: git is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise RepoError(f"Could not clone {source}: {exc.stderr.strip()}") from exc
        yield Path(clone_dir)
    finally:
        _remove_tree(clone_dir)

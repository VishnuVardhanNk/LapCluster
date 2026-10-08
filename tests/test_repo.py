import subprocess
from pathlib import Path

import pytest

from lapclusters.repo import MAX_FILE_BYTES, RepoError, collect_files, open_repo


def _write(root: Path, relative: str, content: str | bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def test_collects_source_files_in_stable_order(tmp_path):
    _write(tmp_path, "src/b.py", "print('b')\n")
    _write(tmp_path, "a.js", "console.log('a')\n")
    collected = collect_files(tmp_path)
    assert [f.path for f in collected.files] == ["a.js", "src/b.py"]
    assert collected.files[1].content == "print('b')\n"
    assert collected.skipped == []


def test_ignores_dependency_folders_and_non_source_files(tmp_path):
    _write(tmp_path, "main.py", "x = 1\n")
    _write(tmp_path, "node_modules/lib/index.js", "x\n")
    _write(tmp_path, ".git/config.py", "x\n")
    _write(tmp_path, ".venv/site.py", "x\n")
    _write(tmp_path, "notes.txt", "hello\n")
    _write(tmp_path, "logo.png", b"\x89PNG")
    collected = collect_files(tmp_path)
    assert [f.path for f in collected.files] == ["main.py"]
    assert collected.skipped == []


def test_ignores_empty_files(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/blank.py", "   \n\n")
    assert collect_files(tmp_path).files == []


def test_reports_oversized_files_as_skipped(tmp_path):
    _write(tmp_path, "big.py", "x = 1\n" * (MAX_FILE_BYTES // 6 + 10))
    collected = collect_files(tmp_path)
    assert collected.files == []
    assert collected.skipped == [("big.py", "larger than 20 KB")]


def test_reports_non_text_source_files_as_skipped(tmp_path):
    _write(tmp_path, "weird.py", b"\xff\xfe\x00\x00binary")
    collected = collect_files(tmp_path)
    assert collected.files == []
    assert collected.skipped == [("weird.py", "not UTF-8 text")]


def test_reports_unreadable_files_as_skipped(tmp_path, monkeypatch):
    _write(tmp_path, "good.py", "x = 1\n")
    _write(tmp_path, "locked.py", "y = 2\n")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "locked.py":
            raise PermissionError("access denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    collected = collect_files(tmp_path)
    assert [f.path for f in collected.files] == ["good.py"]
    assert collected.skipped == [("locked.py", "could not be read")]


def test_open_repo_uses_a_local_folder_as_is(tmp_path):
    with open_repo(str(tmp_path)) as root:
        assert root == tmp_path
    assert tmp_path.exists()


def test_open_repo_rejects_a_missing_folder(tmp_path):
    with pytest.raises(RepoError, match="Not a folder"):
        with open_repo(str(tmp_path / "nope")):
            pass


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_open_repo_clones_a_url_and_cleans_up(tmp_path):
    origin = tmp_path / "origin"
    _write(origin, "app.py", "print('hi')\n")
    _git(origin, "init", "-q")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "first")

    with open_repo(origin.as_uri()) as root:
        clone_dir = root
        assert root != origin
        assert [f.path for f in collect_files(root).files] == ["app.py"]
    assert not clone_dir.exists()


def test_open_repo_reports_a_failed_clone(tmp_path):
    missing = (tmp_path / "missing").as_uri()
    with pytest.raises(RepoError, match="Could not clone"):
        with open_repo(missing):
            pass

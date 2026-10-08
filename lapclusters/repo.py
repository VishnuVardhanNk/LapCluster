"""Getting a collection of files onto this laptop and preparing it as tasks:
choosing files, splitting long ones, reading PDFs and pictures."""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from lapclusters import config

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
    "node_modules", "__pycache__", "dist", "build", "target", "vendor",
}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".sql",
}
TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".csv",
    ".tsv", ".html", ".htm", ".xml", ".css", ".ini", ".cfg", ".log", ".tex",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
PDF_EXTENSION = ".pdf"

REVIEW = "review"
ASK = "ask"

# Sanity limits. Anything larger is listed as skipped rather than read.
MAX_TEXT_BYTES = 3_000_000
MAX_IMAGE_BYTES = 25_000_000
MAX_PDF_BYTES = 60_000_000
# A picture is shrunk so its longer side is at most this, which keeps what
# travels through Redis small without losing what a model can make out.
IMAGE_SIDE = 1280
# Pictures sent along with one piece of text, at most.
PICTURES_PER_PART = 4
# Lines repeated at the start of each part of a split file, so that something
# straddling a boundary is seen whole at least once.
OVERLAP_LINES = 10
URL_PREFIXES = ("http://", "https://", "git@", "ssh://", "file://")

_PICTURE_LINK = re.compile(
    r"""(?:!\[[^\]]*\]\(\s*<?([^)\s>]+)|<img[^>]+src\s*=\s*["']([^"']+)|\\includegraphics(?:\[[^\]]*\])?\{([^}]+))""",
    re.IGNORECASE,
)


class RepoError(Exception):
    pass


@dataclass
class SourceFile:
    """One task's worth of a file: all of a short file, or one part of a long one."""

    path: str
    content: str = ""
    # "lines 401-800" or "part 2 of 5"; empty when this is the whole file.
    part: str = ""
    # Line number in the whole file of the first line of `content`.
    first_line: int = 1
    kind: str = "text"  # "text" or "image"
    # Base64 JPEGs: the picture itself, or pictures that belong to this text.
    images: list[str] = field(default_factory=list)
    # A few words about what sits next to this file, to keep it in context.
    context: str = ""

    @property
    def name(self) -> str:
        return f"{self.path} ({self.part})" if self.part else self.path


@dataclass
class Collected:
    files: list[SourceFile] = field(default_factory=list)
    # (path, reason) for files that could not be sent
    skipped: list[tuple[str, str]] = field(default_factory=list)


# --- splitting ---------------------------------------------------------------


def split_text(content: str, part_chars: int, overlap_lines: int = OVERLAP_LINES) -> list[tuple[int, int, str]]:
    """Cut text into parts of at most about `part_chars` characters.

    Returns (first line, last line, text) for each part, with line numbers
    counted from 1. Consecutive parts overlap by a few lines. A single line
    longer than a whole part, as in minified code, is cut into pieces.
    """
    part_chars = max(part_chars, 200)
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(content.splitlines(), start=1):
        if len(line) <= part_chars:
            lines.append((number, line))
        else:
            lines += [(number, line[i:i + part_chars]) for i in range(0, len(line), part_chars)]
    if not lines:
        return []

    parts = []
    start = 0
    while start < len(lines):
        size = 0
        end = start
        while end < len(lines) and (end == start or size + len(lines[end][1]) + 1 <= part_chars):
            size += len(lines[end][1]) + 1
            end += 1
        chunk = lines[start:end]
        parts.append((chunk[0][0], chunk[-1][0], "\n".join(text for _, text in chunk)))
        if end >= len(lines):
            break
        # Step back for the overlap, never by more than a quarter of the part,
        # so that parts made of a few very long lines still move forward.
        start = end - min(overlap_lines, (end - start) // 4)
    return parts


# --- pictures ----------------------------------------------------------------


def encode_image(data: bytes) -> str | None:
    """Shrink a picture and return it as a base64 JPEG, or None if it cannot
    be read. Needs the Pillow package."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85)
    except Exception:  # Pillow raises many kinds of error for bad pictures
        return None
    return base64.b64encode(out.getvalue()).decode("ascii")


def _linked_pictures(text: str, folder: Path, root: Path) -> list[Path]:
    """Picture files a document refers to, such as the figures of a Markdown file."""
    found = []
    for match in _PICTURE_LINK.finditer(text):
        target = next(group for group in match.groups() if group)
        if target.startswith(("http://", "https://", "data:")):
            continue
        candidate = (folder / target.split("#")[0].split("?")[0]).resolve()
        try:
            candidate.relative_to(root.resolve())  # never read outside the collection
        except ValueError:
            continue
        if candidate.suffix.lower() in IMAGE_EXTENSIONS and candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


# --- PDFs ----------------------------------------------------------------------


def read_pdf(path: Path) -> tuple[str, list[str]]:
    """The text of a PDF with a marker before each page, and its larger
    pictures as base64 JPEGs. Needs the pypdf package. Raises ValueError when
    the file cannot be read as a PDF."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ValueError("password-protected")
        pages = []
        pictures: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[page {number}]\n{text}")
            if len(pictures) < 3 * PICTURES_PER_PART:
                try:
                    for picture in page.images:
                        if len(picture.data) < 8_000:
                            continue  # icons, rules and other decoration
                        encoded = encode_image(picture.data)
                        if encoded:
                            pictures.append(encoded)
                except Exception:
                    pass  # a picture that cannot be decoded is not worth failing the file for
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("could not be read as a PDF") from exc
    return "\n\n".join(pages), pictures


# --- collecting ----------------------------------------------------------------


def _parts(
    relative: str, text: str, part_chars: int, by_lines: bool, pictures: list[str], context: str
) -> list[SourceFile]:
    pieces = split_text(text, part_chars)
    files = []
    for index, (first, last, chunk) in enumerate(pieces, start=1):
        if len(pieces) == 1:
            label = ""
        elif by_lines:
            label = f"lines {first}-{last}"
        else:
            label = f"part {index} of {len(pieces)}"
        files.append(
            SourceFile(
                # A file that fits in one task is sent exactly as it is.
                relative, text if len(pieces) == 1 else chunk, part=label,
                first_line=first if by_lines else 1,
                # A document's pictures go with its first part.
                images=pictures[:PICTURES_PER_PART] if index == 1 else [],
                context=context,
            )
        )
    return files


def _neighbours(folder_files: list[str], this: str) -> str:
    others = [name for name in folder_files if name != this][:12]
    return "Other files in the same folder: " + ", ".join(others) if others else ""


def collect_files(
    root: Path | str, mode: str = REVIEW, part_chars: int | None = None, pictures: bool = True
) -> Collected:
    """Walk a folder and prepare its files as tasks.

    `mode` REVIEW takes source code only. ASK also takes documents, PDFs and
    pictures. With `pictures` False, pictures are left out (for a cluster in
    which no laptop's model can see).
    """
    root = Path(root)
    part_chars = part_chars or config.PART_CHARS
    collected = Collected()
    wanted = set(SOURCE_EXTENSIONS)
    if mode == ASK:
        wanted |= TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {PDF_EXTENSION}

    for folder, subfolders, names in os.walk(root):
        subfolders[:] = sorted(d for d in subfolders if d not in SKIP_DIRS)
        # Documents before pictures, so a picture can be given the text that
        # refers to it.
        names = sorted(names, key=lambda n: (Path(n).suffix.lower() in IMAGE_EXTENSIONS, n))
        texts_here: dict[str, str] = {}
        for name in names:
            full = Path(folder) / name
            suffix = full.suffix.lower()
            if suffix not in wanted:
                continue
            relative = full.relative_to(root).as_posix()
            try:
                size = full.stat().st_size
                if suffix in IMAGE_EXTENSIONS:
                    _take_picture(collected, full, relative, size, names, texts_here, pictures)
                elif suffix == PDF_EXTENSION:
                    _take_pdf(collected, full, relative, size, part_chars, names, pictures)
                else:
                    text = _take_text(
                        collected, full, relative, size, part_chars, names, root,
                        by_lines=True, with_pictures=pictures and mode == ASK,
                    )
                    if text and suffix in TEXT_EXTENSIONS:
                        texts_here[name] = text
            except OSError:
                # For example a broken symlink or a file we may not open.
                collected.skipped.append((relative, "could not be read"))
    collected.files.sort(key=lambda f: (f.path, f.first_line, f.part))
    return collected


def _take_text(collected, full, relative, size, part_chars, names, root, by_lines, with_pictures):
    if size > MAX_TEXT_BYTES:
        collected.skipped.append((relative, f"larger than {MAX_TEXT_BYTES // 1_000_000} MB"))
        return ""
    try:
        text = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        collected.skipped.append((relative, "not UTF-8 text"))
        return ""
    if not text.strip():
        return ""
    attached = []
    if with_pictures:
        for picture in _linked_pictures(text, full.parent, root)[:PICTURES_PER_PART]:
            encoded = encode_image(picture.read_bytes())
            if encoded:
                attached.append(encoded)
    context = _neighbours(names, full.name) if with_pictures else ""
    collected.files += _parts(relative, text, part_chars, by_lines, attached, context)
    return text


def _take_pdf(collected, full, relative, size, part_chars, names, pictures):
    if size > MAX_PDF_BYTES:
        collected.skipped.append((relative, f"larger than {MAX_PDF_BYTES // 1_000_000} MB"))
        return
    try:
        text, found = read_pdf(full)
    except ValueError as exc:
        collected.skipped.append((relative, str(exc)))
        return
    if len(text.strip()) < 20:
        collected.skipped.append((relative, "no text could be extracted (a scanned PDF?)"))
        return
    collected.files += _parts(
        relative, text, part_chars, False, found if pictures else [], _neighbours(names, full.name)
    )


def _take_picture(collected, full, relative, size, names, texts_here, pictures):
    if not pictures:
        collected.skipped.append((relative, "no laptop in the cluster has a model that can see"))
        return
    if size > MAX_IMAGE_BYTES:
        collected.skipped.append((relative, f"larger than {MAX_IMAGE_BYTES // 1_000_000} MB"))
        return
    encoded = encode_image(full.read_bytes())
    if encoded is None:
        collected.skipped.append((relative, "could not be read as a picture"))
        return
    # Give the picture the text that surrounds it: a document in the same
    # folder that mentions it, so the two keep their shared meaning.
    context = [_neighbours(names, full.name)]
    for name, text in texts_here.items():
        position = text.find(full.name)
        if position >= 0:
            excerpt = " ".join(text[max(position - 500, 0):position + 500].split())
            context.append(f"{name} refers to this picture. Around that reference it says: {excerpt}")
            break
    collected.files.append(
        SourceFile(relative, kind="image", images=[encoded], context="\n".join(c for c in context if c))
    )


# --- fetching ------------------------------------------------------------------


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

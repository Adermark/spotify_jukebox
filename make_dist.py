"""Build a zip to share with others: python make_dist.py  ->  dist/jukebox-YYYY-MM-DD.zip

Only the files listed below go in, so personal files (.env with your Spotify secret and
admin password, jukebox.db with your Spotify login and history, .venv) can never slip in.
Docker files and tests are left out, and so are the README's developer sections.
"""
import datetime
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INCLUDE = ["START HERE.txt", "README.md", "run.bat", "run.sh", "run.command", "run.py", "requirements.txt",
           ".env.example", "app", "static"]
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db"}
README_SECTIONS_TO_DROP = ("Docker", "Development", "Tests", "Making a zip to share", "Contributing")


def files() -> list[Path]:
    out = []
    for name in INCLUDE:
        path = ROOT / name
        if path.is_dir():
            out += [p for p in sorted(path.rglob("*")) if p.is_file()
                    and not SKIP_DIRS & set(p.relative_to(ROOT).parts) and p.suffix not in SKIP_SUFFIXES]
        else:
            out.append(path)
    return out


def readme() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for title in README_SECTIONS_TO_DROP:
        text = re.sub(rf"^## {title}\n.*?(?=^## |\Z)", "", text, flags=re.S | re.M)
    text = re.sub(r"^\s*\*\*Docker:\*\*.*\n\n?", "", text, flags=re.M)  # the pointer in Setup
    return text


def contents(path: Path) -> bytes:
    if path.name == "README.md":
        data = readme().encode()
    else:
        data = path.read_bytes()
    if path.suffix in (".bat", ".txt"):
        # Windows line endings: cmd.exe can mis-handle labels/goto in LF-only batch files.
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    elif path.suffix in (".sh", ".command"):
        data = data.replace(b"\r\n", b"\n")
    return data


def main() -> Path:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"jukebox-{datetime.date.today().isoformat()}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files():
            info = zipfile.ZipInfo(f"jukebox/{path.relative_to(ROOT).as_posix()}",
                                   date_time=datetime.datetime.now().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            # Unix permissions only count when the entry says it was made on Unix (3); Python on
            # Windows says DOS (0), and then macOS/Linux unzip ignores the executable bit.
            info.create_system = 3
            executable = path.suffix in (".sh", ".command")
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            zf.writestr(info, contents(path))
    print(f"Built {target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB, {len(files())} files)")
    return target


if __name__ == "__main__":
    main()

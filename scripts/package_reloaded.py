"""Build the installable plugin zip for a Krita AI Reloaded release.

Usage: python scripts/package_reloaded.py [git-ref]   (default: HEAD)

Unlike scripts/package.py (upstream's), this needs only the standard library and git.
Plugin files come from `git archive <ref>`, so untracked leftovers in the working tree
never end up in the zip. The bundled websockets library is gitignored and is copied
from the local ai_diffusion/websockets folder.
"""

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

root = Path(__file__).parent.parent
websockets_dir = root / "ai_diffusion" / "websockets"
skip_parts = {"__pycache__", ".git", ".github"}


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def build(ref: str):
    if not (websockets_dir / "src" / "websockets").is_dir():
        sys.exit(f"Missing {websockets_dir / 'src' / 'websockets'} - restore it from a release")

    name = git("describe", "--tags", "--always", ref).decode().strip()
    out = root / f"krita_ai_reloaded-{name}.zip"
    tar = git("archive", "--format=tar", ref, "ai_diffusion", "ai_diffusion.desktop", "LICENSE")
    extra = git("show", f"{ref}:scripts/download_models.py")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                data = tf.extractfile(member).read()  # type: ignore
                path = "ai_diffusion/LICENSE" if member.name == "LICENSE" else member.name
                zf.writestr(path, data)
        zf.writestr("ai_diffusion/download_models.py", extra)
        for file in sorted(websockets_dir.rglob("*")):
            rel = file.relative_to(root)
            if file.is_file() and not skip_parts.intersection(rel.parts) and file.suffix != ".pyc":
                zf.write(file, rel.as_posix())

    print(out)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "HEAD")

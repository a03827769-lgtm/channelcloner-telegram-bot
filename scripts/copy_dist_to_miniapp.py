import shutil
from pathlib import Path

src = Path("webapp/dist")
dst = Path("miniapp/public")

if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst)
files = list(dst.rglob("*"))
print(f"Successfully copied {len(files)} files to {dst}")

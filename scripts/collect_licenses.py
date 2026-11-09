"""Collect exact license files from the distributions used to build the EXE."""
from importlib import metadata
from pathlib import Path
import shutil
import sys

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
for name in ("tree-sitter", "tree-sitter-java", "pyinstaller", "certifi"):
    distribution = metadata.distribution(name)
    for file in distribution.files or []:
        if "LICENSE" in file.name.upper() or "COPYING" in file.name.upper():
            path = distribution.locate_file(file)
            if path.is_file():
                shutil.copyfile(path, output / (name + "-" + file.name))
for name in ("LICENSE.txt", "LICENSE", "LICENSE.PYTHON", "LICENSE_PYTHON.txt"):
    path = Path(sys.base_prefix) / name
    if path.is_file():
        shutil.copyfile(path, output / "Python-LICENSE.txt")
        break
else:
    raise SystemExit("Python license file not found; collect it before distribution")

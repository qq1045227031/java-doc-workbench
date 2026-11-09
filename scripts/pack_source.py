from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
output = root / "dist" / "JavaDescriptionWorkbench-Source.zip"
files = [root / name for name in ["README.md", "app.py", "requirements.txt", ".gitignore", "start.cmd", "build.ps1", "使用说明.md", "测试报告.md", "THIRD_PARTY_NOTICES.txt"]]
for folder in ["workbench", "static", "tests", "scripts", "artifacts"]:
    files.extend(p for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, "swagger-workbench/" + path.relative_to(root).as_posix())
    for path in (root / "dist" / "licenses").glob("*"):
        if path.is_file():
            archive.write(path, "swagger-workbench/licenses/" + path.name)
print(output)

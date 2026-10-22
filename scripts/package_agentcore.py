"""Build a locked, Linux ARM64 deployment ZIP without Docker or AWS calls."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/agentcore.zip")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="support-agent-package-") as temporary:
        temporary = Path(temporary)
        requirements = temporary / "requirements.txt"
        package = temporary / "package"
        subprocess.run([
            "uv", "export", "--frozen", "--no-dev", "--extra", "agentcore", "--no-emit-project",
            "--format", "requirements-txt", "--output-file", str(requirements),
        ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([
            "uv", "pip", "install", "--python-platform", "aarch64-manylinux_2_28",
            "--python-version", "3.13", "--only-binary=:all:", "--require-hashes",
            "--target", str(package), "-r", str(requirements),
        ], cwd=ROOT, check=True)
        shutil.copytree(ROOT / "src/customer_support", package / "customer_support",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(ROOT / "agentcore/main.py", package / "main.py")
        shutil.copyfile(ROOT / "LICENSE", package / "PROJECT_LICENSE")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                content = path.read_bytes()
                total += len(content)
                info = zipfile.ZipInfo(path.relative_to(package).as_posix())
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        if total > 750 * 1024**2 or args.output.stat().st_size > 250 * 1024**2:
            raise SystemExit("Deployment archive exceeds AgentCore ZIP size limits")
    print(f"Created {args.output} ({args.output.stat().st_size:,} bytes; Python 3.13, Linux ARM64)")


if __name__ == "__main__":
    main()

"""Check release integrity, path safety, dataset hashes and secret-file exclusions."""

import hashlib
import json
import zipfile
from pathlib import PurePosixPath

from src.config import PROJECT_ROOT


def main():
    output = PROJECT_ROOT / "dist"
    sums = json.loads((output / "SHA256SUMS.json").read_text())
    for name, expected in sums.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected, name
        with zipfile.ZipFile(output / name) as archive:
            assert archive.testzip() is None
            for entry in archive.namelist():
                path = PurePosixPath(entry)
                assert not path.is_absolute() and ".." not in path.parts
                assert not ({".venv", "node_modules", "runtime", ".git"} & set(path.parts))
                assert path.name != ".env" and not path.name.endswith(".sqlite3")
    with zipfile.ZipFile(output / "mock-dataset-v1.0.0.zip") as archive:
        prefix = "mock-dataset-v1.0.0/"
        manifest = json.loads(archive.read(prefix + "manifest.json"))
        for name, expected in manifest["sha256"].items():
            assert hashlib.sha256(archive.read(prefix + name)).hexdigest() == expected, name
    with zipfile.ZipFile(output / "operations-agent-harness-v0.1.0.zip") as archive:
        image_names = {
            PurePosixPath(name).name for name in archive.namelist() if name.endswith(".png")
        }
        assert image_names == {
            "operations-layered-architecture-en-v2.png",
            "operations-business-workflow-en-v1.png",
        }
    print(
        json.dumps(
            {
                "verified_archives": list(sums),
                "dataset_hashes": len(manifest["sha256"]),
                "excluded_secret_and_runtime_files": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

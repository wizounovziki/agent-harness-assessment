"""Produce sample traces, dataset checksums and shareable release archives."""

import asyncio
import hashlib
import importlib.metadata
import json
import platform
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from src.config import PROJECT_ROOT, Settings
from src.service import RunManager


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def examples(root: Path) -> list[dict]:
    evidence = root / "docs" / "examples"
    evidence.mkdir(exist_ok=True)
    records = []
    for label, scenario, approval in [
        ("healthy", "healthy", None),
        ("incident", "incident", True),
        ("retry", "retry", None),
        ("persistent_failure", "persistent_failure", None),
        ("loop", "loop", None),
        ("rejected", "incident", False),
        ("deploy_success", "deploy_success", True),
        ("deploy_capacity_blocked", "deploy_capacity_blocked", None),
        ("deploy_rollback", "deploy_rollback", True),
        ("deploy_late_failure", "deploy_late_failure", True),
        ("deploy_rollback_failure", "deploy_rollback_failure", True),
    ]:
        settings = Settings(
            database_path=root / "runtime" / "examples" / f"{label}.sqlite3",
            trace_dir=root / "runtime" / "examples" / "traces",
        )
        manager = RunManager(settings)
        try:
            callback = (lambda draft, choice=approval: choice) if approval is not None else None
            harness = manager.harness_for(scenario, approval_callback=callback)
            state = harness.new_state(
                manager.dataset.scenario(scenario)["objective"],
                scenario_id=scenario,
                owner_id="example-user",
            )
            state = await harness.run(state)
            shutil.copyfile(manager.tracer.path(state.session_id), evidence / f"{label}.jsonl")
            records.append(
                {
                    "scenario": label,
                    "session_id": state.session_id,
                    "status": state.status.value,
                    "stop_reason": state.stop_reason,
                    "steps": state.current_step,
                    "tool_attempts": state.tool_attempts,
                    "incident_count": len(manager.store.incidents(state.session_id)),
                    "final_answer": state.final_answer,
                    "changes": harness.registry.tools.operations.changes(state.session_id),
                }
            )
        finally:
            await manager.close()
    (evidence / "summary.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return records


def main():
    root = PROJECT_ROOT
    output = root / "dist"
    output.mkdir(exist_ok=True)
    records = asyncio.run(examples(root))
    dataset = root / "data"
    files = [file for file in dataset.rglob("*") if file.is_file() and file.name != "manifest.json"]
    manifest = {
        "version": "1.1.0",
        "services": 3,
        "knowledge_documents": 4,
        "scenarios": 21,
        "deployment_environments": 2,
        "registered_tools": 8,
        "license": "MIT",
        "sha256": {
            str(file.relative_to(dataset)).replace("\\", "/"): sha256(file)
            for file in sorted(files)
        },
    }
    (dataset / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    junit = root / "runtime" / "test-results.xml"
    test_result = {"executed": False}
    if junit.exists():
        suites = ET.parse(junit).getroot()
        test_result = {
            "executed": True,
            **{
                name: sum(int(suite.attrib.get(name, 0)) for suite in suites.findall("testsuite"))
                for name in ("tests", "failures", "errors", "skipped")
            },
        }
        shutil.copyfile(junit, root / "docs/examples/test-results.xml")
    newman_path = root / "runtime" / "postman-results.json"
    newman = {"executed": False}
    if newman_path.exists():
        report = json.loads(newman_path.read_text(encoding="utf-8"))
        newman = {
            "executed": True,
            "stats": report["run"]["stats"],
            "failures": len(report["run"]["failures"]),
        }
    live_path = root / "runtime" / "live-model-smoke.json"
    live = (
        json.loads(live_path.read_text(encoding="utf-8"))
        if live_path.exists()
        else {"executed": False, "status": "requires local credentials and model selection"}
    )
    clean_path = root / "runtime/clean-install-results.json"
    clean = json.loads(clean_path.read_text()) if clean_path.exists() else {"executed": False}
    fingerprint_files = [
        path
        for folder in ("src", "tests", "scripts", "data")
        for path in (root / folder).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json"}
        and "__pycache__" not in path.parts
        and path.name != "manifest.json"
    ]
    fingerprint_files.extend(root / name for name in ("main.py", "pyproject.toml", "uv.lock"))
    source_hashes = {
        str(path.relative_to(root)).replace("\\", "/"): sha256(path)
        for path in sorted(fingerprint_files)
    }
    fingerprint = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
    publication_path = root / "docs/publication.json"
    publication = json.loads(publication_path.read_text()) if publication_path.exists() else {}
    results = {
        "source_fingerprint": fingerprint,
        "source_sha256": source_hashes,
        "python": platform.python_version(),
        "platform": platform.system(),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("fastapi", "pydantic", "openai", "pytest", "uvicorn")
        },
        "automated_tests": test_result,
        "postman": newman,
        "clean_install": clean,
        "examples": records,
        "live_model": live,
        "live_change_model": json.loads(
            (root / "docs/live-change-results.json").read_text(encoding="utf-8")
        )
        if (root / "docs/live-change-results.json").exists()
        else {"executed": False},
        "credential_scan": json.loads(
            (root / "docs/credential-scan.json").read_text(encoding="utf-8")
        )
        if (root / "docs/credential-scan.json").exists()
        else {"executed": False},
        "public_repository": publication.get("repository_url"),
        "public_dataset_url": publication.get("dataset_url"),
        "publication_status": publication.get("status", "unpublished"),
        "public_access_verified": publication.get("public_access_verified", False),
    }
    (root / "docs" / "validation-results.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    data_zip = output / "mock-dataset-v1.1.0.zip"
    with zipfile.ZipFile(data_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(dataset.rglob("*")):
            if file.is_file():
                archive.write(file, str(Path("mock-dataset-v1.1.0") / file.relative_to(dataset)))
        archive.write(root / "LICENSE", "mock-dataset-v1.1.0/LICENSE")
    source_zip = output / "operations-agent-harness-v0.1.0.zip"
    source_files = [
        file
        for folder in ("src", "tests", "scripts", "data", "docs", ".github")
        if (root / folder).exists()
        for file in (root / folder).rglob("*")
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc"
    ]
    source_files.extend(
        root / name
        for name in (
            "main.py",
            "pyproject.toml",
            "uv.lock",
            "requirements.txt",
            "README.md",
            "LICENSE",
            ".env.example",
            ".gitignore",
            "postman_collection.json",
            "postman_environment.json",
        )
    )
    selected_images = {
        "operations-layered-architecture-en-v3.png",
        "operations-business-workflow-en-v2.png",
    }
    source_files = [
        file
        for file in source_files
        if (file.suffix.lower() != ".png" or file.name in selected_images)
        and file.name != "image-generation-prompts.md"
    ]
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source_files):
            archive.write(file, str(Path("operations-agent-harness") / file.relative_to(root)))
    sums = {file.name: sha256(file) for file in (data_zip, source_zip)}
    (output / "SHA256SUMS.json").write_text(json.dumps(sums, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"archives": sums, "tests": test_result, "postman": newman}, indent=2))


if __name__ == "__main__":
    main()

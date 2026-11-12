"""Export the API contract without starting a model client or server."""

import json

from src.api import create_app
from src.config import PROJECT_ROOT, Settings


def main():
    contract = create_app(Settings(llm_mode="scripted")).openapi()
    path = PROJECT_ROOT / "docs/openapi.json"
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(contract['paths'])} API paths to docs/openapi.json")


if __name__ == "__main__":
    main()

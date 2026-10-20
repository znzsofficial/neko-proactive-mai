"""Smoke-test a source plugin archive, not a wheel or production release."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
# Deliberately exclude local configuration, capability keys and runtime outbox.
FILES = (
    "_manifest.json",
    "plugin.py",
    "config.py",
    "life_bridge.py",
    "prompt_visibility.py",
    "README.md",
    "LICENSE",
    "NOTICE.md",
)


def main():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "plugin.zip"
        with ZipFile(archive, "w") as bundle:
            for name in FILES:
                bundle.write(ROOT / name, f"neko-proactive-mai/{name}")
        with ZipFile(archive) as bundle:
            assert bundle.testzip() is None
            bundle.extractall(Path(directory) / "extracted")
        extracted = Path(directory) / "extracted" / "neko-proactive-mai"
        manifest = json.loads((extracted / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["id"] == "neko.proactive"
        package = types.ModuleType("archive_smoke")
        package.__path__ = [str(extracted)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location(
            "archive_smoke.plugin", extracted / "plugin.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plugin = module.create_plugin()
        assert plugin.config_model is module.LonelyMaiConfig
        names = {component["name"] for component in plugin.get_components()}
        assert {
            "lonelymai_trigger",
            "lonelymai_latest_planner_task",
            "lonelymai_latest_replyer_task",
            "lonelymai_life_receipt",
        } <= names
        assert not (extracted / "config.toml").exists()
        print("Source archive extracted; SDK discovers plugin factory/config without local config")


if __name__ == "__main__":
    main()

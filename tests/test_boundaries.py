import ast
from pathlib import Path


ROOT = Path(__file__).parents[1] / "wikiroutes" / "overture"


def test_host_imports_are_isolated_in_adapters():
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.name == "adapters.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level >= 2:
                raise AssertionError(f"host import leaked into {path.name}: {node}")


def test_loader_does_not_depend_on_download_layer():
    tree = ast.parse(
        (ROOT / "load.py").read_text(encoding="utf-8"),
        filename=str(ROOT / "load.py"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "download"

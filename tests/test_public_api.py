def test_public_api_is_lazy():
    import overture

    assert "compute_overture_result" in overture.__all__
    from overture import OvertureConfig

    assert OvertureConfig.cache_version == 12


def test_legacy_poi_loader_import_is_preserved():
    from overture.load import resolve_poi_place_file

    assert callable(resolve_poi_place_file)


def test_shared_helpers_have_single_implementation():
    from overture import download, http, load, poi

    assert download._sql_literal is http._sql_literal
    assert download.resolve_poi_place_file is poi.resolve_poi_place_file
    assert load.resolve_poi_place_file is poi.resolve_poi_place_file


def test_no_duplicate_top_level_function_definitions():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[1]
    locations = {}
    for path in root.rglob("*.py"):
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                locations.setdefault(node.name, []).append(str(path.relative_to(root)))
    duplicates = {name: paths for name, paths in locations.items() if len(paths) > 1}
    assert duplicates == {}

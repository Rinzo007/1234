from overture.download import _download_backend_order


def test_download_backend_auto_cascade():
    # HTTP идёт первым: транспорт распараллелен и устойчивее к обрывам TLS,
    # чем прямой DuckDB-скан облачных объектов. DuckDB-пути остаются как
    # запасные, чтобы «auto» отработал и при провале основного транспорта.
    assert _download_backend_order("auto") == ["http", "duckdb_s3", "duckdb_azure"]


def test_download_backend_aliases():
    assert _download_backend_order("duckdb_s3") == ["duckdb_s3"]
    assert _download_backend_order("duckdb_azure") == ["duckdb_azure"]
    assert _download_backend_order("http") == ["http"]


def test_download_backend_invalid():
    import pytest

    with pytest.raises(ValueError):
        _download_backend_order("ftp")

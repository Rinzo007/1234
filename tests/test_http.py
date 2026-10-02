import pytest

from overture.http import (
    _http_get_range,
    _is_valid_cached_part,
    _sha256_file,
    _write_part_manifest,
)


class _FakeResponse:
    def __init__(self, *, status, data, headers):
        self.status = status
        self._data = data
        self.headers = headers

    def read(self, _size=None):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_range_request_is_exact(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout, context):
        captured["range"] = request.headers["Range"]
        captured["timeout"] = timeout
        return _FakeResponse(
            status=206,
            data=b"efgh",
            headers={"Content-Range": "bytes 4-7/10"},
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, data, total = _http_get_range(
        "https://example.test/object",
        start=4,
        timeout=5,
        chunk=4,
        context=None,
    )
    assert captured["range"] == "bytes=4-7"
    assert status == 206
    assert data == b"efgh"
    assert total == 10


def test_invalid_content_range_is_rejected(monkeypatch):
    def fake_urlopen(request, timeout, context):
        return _FakeResponse(
            status=206,
            data=b"efgh",
            headers={"Content-Range": "bytes 5-8/10"},
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(OSError):
        _http_get_range(
            "https://example.test/object",
            start=4,
            timeout=5,
            chunk=4,
            context=None,
        )


def test_part_manifest_detects_tampering(tmp_path):
    pytest.importorskip("pyarrow")
    path = tmp_path / "part.parquet"
    import pyarrow.parquet as pq
    import pyarrow as pa

    pq.write_table(pa.table({"id": [1, 2]}), path)
    digest = _sha256_file(path)
    _write_part_manifest(path, key="bucket/object", size=path.stat().st_size, sha256=digest)

    assert _is_valid_cached_part(path, key="bucket/object")
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    assert not _is_valid_cached_part(path, key="bucket/object")


def test_fetch_chunk_attempts_at_least_once_when_retries_zero(monkeypatch):
    calls = []

    def fake_range(url, start, timeout, chunk, context):
        calls.append((url, start))
        return 206, b"x", 1

    monkeypatch.setattr("overture.http._http_get_range", fake_range)
    from overture.http import _fetch_chunk

    result = _fetch_chunk(
        ["https://a.test/object", "https://b.test/object"],
        start=0,
        timeout=5,
        chunk=1,
        retries=0,
    )
    assert result == (206, b"x", 1)
    assert calls == [("https://a.test/object", 0)]


def test_overture_http_includes_official_azure_mirrors():
    from overture.http import _OVERTURE_HTTP_HOSTS, _overture_host_url

    assert "https://overturemapswestus2.blob.core.windows.net" in _OVERTURE_HTTP_HOSTS
    assert "https://overturemapswestus2.dfs.core.windows.net" in _OVERTURE_HTTP_HOSTS

    key = "overturemaps-us-west-2/release/2026-01-21.0/theme=buildings/type=building/part.parquet"
    bucket, _, obj_path = key.partition("/")

    blob = _overture_host_url(
        "https://overturemapswestus2.blob.core.windows.net",
        bucket,
        key,
        obj_path,
    )
    dfs = _overture_host_url(
        "https://overturemapswestus2.dfs.core.windows.net",
        bucket,
        key,
        obj_path,
    )
    assert blob.endswith("/release/2026-01-21.0/theme=buildings/type=building/part.parquet")
    assert dfs.endswith("/release/2026-01-21.0/theme=buildings/type=building/part.parquet")


def test_stac_request_closes_connection(monkeypatch):
    import overture.http as http

    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"stac"

    def fake_urlopen(request, timeout, context):
        captured.append((request.headers.get("Connection"), timeout, context))
        return Response()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    context = object()
    assert http._http_get_url("https://stac.overturemaps.org/x.parquet", 120.0, context) == b"stac"
    assert captured == [("close", 120.0, context)]


def test_stac_downloads_ranges_and_passes_retry_budget(monkeypatch):
    import overture.http as http

    calls = []

    def fake_fetch_chunk(url, start, timeout, chunk, retries, retry_delay):
        calls.append((start, timeout, chunk, retries))
        if start == 0:
            return 206, b"abcd", 8
        return 206, b"efgh", 8

    monkeypatch.setattr(http, "_fetch_chunk", fake_fetch_chunk)
    monkeypatch.setattr(http, "_STAC_CHUNK_BYTES", 4)

    assert http._http_get_stac(
        "https://stac.overturemaps.org/x.parquet",
        timeout=7,
        retries=1,
        retry_delay=0,
    ) == b"abcdefgh"
    assert calls[0][0] == 0
    assert calls[1][0] == 4
    assert all(call[1] == 7 for call in calls)
    assert all(call[3] == 1 for call in calls)

def test_stac_collection_json_fallback_filters_by_bbox(monkeypatch):
    import json
    import overture.http as http

    collection_url = "https://stac.overturemaps.org/2026-08-19.0/places/place/collection.json"
    collection = {
        "extent": {
            "spatial": {
                "bbox": [
                    [-10.0, -10.0, 20.0, 20.0],
                    [37.0, 54.0, 38.0, 55.0],
                    [40.0, 56.0, 41.0, 57.0],
                ]
            }
        },
        "links": [
            {"rel": "item", "href": "./part-a/part-a.json"},
            {"rel": "item", "href": "./part-b/part-b.json"},
        ],
    }
    item_a = {
        "assets": {
            "aws": {
                "alternate": {
                    "s3": {
                        "href": "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/part-a.parquet"
                    }
                }
            }
        }
    }
    item_b = {
        "assets": {
            "aws": {
                "alternate": {
                    "s3": {
                        "href": "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/part-b.parquet"
                    }
                }
            }
        }
    }

    payloads = {
        collection_url: collection,
        "https://stac.overturemaps.org/2026-08-19.0/places/place/part-a/part-a.json": item_a,
        "https://stac.overturemaps.org/2026-08-19.0/places/place/part-b/part-b.json": item_b,
    }

    calls = []

    # ``_http_get_stac`` принимает список зеркальных URL (по одному на каждый
    # хост ``_STAC_HTTP_HOSTS``), а не одну строку. Payload выбирается по
    # содержанию URL: collection.json и item.json приходят одинаково —
    # списками, поэтому тип аргумента их не различает.
    def fake_get(urls, timeout, retries, retry_delay):
        calls.append(urls)
        first = urls[0] if isinstance(urls, list) else urls
        if first.endswith("collection.json"):
            return json.dumps(collection).encode()
        for key, payload in payloads.items():
            if first.endswith(key.rsplit("/", 1)[-1]):
                return json.dumps(payload).encode()
        raise AssertionError(f"неожиданный URL: {first}")

    monkeypatch.setattr(http, "_http_get_stac", fake_get)

    keys = http._http_resolve_stac_part_files_via_collection(
        "2026-08-19.0",
        "places",
        "place",
        (54.5, 37.5, 55.5, 38.5),
        retries=1,
        retry_delay=0,
    )

    assert keys == [
        "overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/part-a.parquet"
    ]
    # Вызваны два резолвера: collection.json, затем item'ы. Каждый — со
    # списком зеркал; в списке item'ов префикс collection.json отсутствует.
    assert len(calls) == 2
    assert all(url.endswith("collection.json") for url in calls[0])
    assert calls[1]
    assert all("collection.json" not in url for url in calls[1])
    assert all(
        url.endswith("/part-a/part-a.json") for url in calls[1]
    ), calls[1]



def test_stac_item_href_strips_collection_json_prefix():
    from overture.http import _stac_item_hrefs

    collection = {
        "extent": {"spatial": {"bbox": [[30.0, 50.0, 40.0, 60.0], [37.0, 54.0, 38.0, 55.0]]}},
        "links": [
            {
                "rel": "item",
                "href": "collection.json/2026-08-19.0/transportation/segment/00002/00002.json",
            }
        ],
    }

    assert _stac_item_hrefs(
        collection,
        (54.5, 37.5, 55.5, 38.5),
        "https://stac.overturemaps.org/2026-08-19.0/transportation/segment/collection.json",
    ) == [
        "https://stac.overturemaps.org/2026-08-19.0/transportation/segment/00002/00002.json"
    ]

def test_stac_item_fetch_does_not_include_collection_json(monkeypatch):
    import json
    import overture.http as http

    calls = []
    collection = {
        "extent": {"spatial": {"bbox": [[30.0, 50.0, 40.0, 60.0], [37.0, 54.0, 38.0, 55.0]]}},
        "links": [{"rel": "item", "href": "collection.json/2026-08-19.0/transportation/segment/00002/00002.json"}],
    }
    item = {"assets": {"aws": {"alternate": {"s3": {"href": "s3://bucket/object.parquet"}}}}}

    # И collection.json, и item.json приходят списками зеркальных URL,
    # поэтому тип аргумента их не различает — различаем по имени файла.
    def fake_get_stac(urls, timeout, retries, retry_delay):
        calls.append(urls)
        first = urls[0] if isinstance(urls, list) else urls
        if first.endswith("collection.json"):
            return json.dumps(collection).encode()
        return json.dumps(item).encode()

    monkeypatch.setattr(http, "_http_get_stac", fake_get_stac)
    keys = http._http_resolve_stac_part_files_via_collection(
        "2026-08-19.0", "transportation", "segment",
        (54.5, 37.5, 55.5, 38.5), retries=0, retry_delay=0,
    )
    assert keys == ["bucket/object.parquet"]
    item_urls = calls[1]
    assert all("collection.json" not in url for url in item_urls)
    assert item_urls[0].endswith("/2026-08-19.0/transportation/segment/00002/00002.json")


def test_stac_fetch_normalizes_duplicate_release_and_collection_json(monkeypatch):
    import json
    import overture.http as http

    calls = []
    collection = {
        "extent": {"spatial": {"bbox": [[30.0, 50.0, 40.0, 60.0], [37.0, 54.0, 38.0, 55.0]]}},
        "links": [{"rel": "item", "href": "./2026-08-19.0/transportation/segment/00000/00000.json"}],
    }
    item = {"assets": {"aws": {"alternate": {"s3": {"href": "s3://bucket/object.parquet"}}}}}

    # И collection.json, и item.json приходят списками зеркальных URL,
    # поэтому тип аргумента их не различает — различаем по имени файла.
    def fake_get_stac(urls, timeout, retries, retry_delay):
        calls.append(urls)
        first = urls[0] if isinstance(urls, list) else urls
        if first.endswith("collection.json"):
            return json.dumps(collection).encode()
        return json.dumps(item).encode()

    monkeypatch.setattr(http, "_http_get_stac", fake_get_stac)
    keys = http._http_resolve_stac_part_files_via_collection(
        "2026-08-19.0", "transportation", "segment",
        (54.5, 37.5, 55.5, 38.5), retries=0, retry_delay=0,
    )
    assert keys == ["bucket/object.parquet"]
    assert calls[1][0].endswith("/2026-08-19.0/transportation/segment/00000/00000.json")
    assert "collection.json" not in calls[1][0]


def test_stac_resolver_falls_back_to_collection_json(monkeypatch):
    import overture.http as http

    calls = []

    def fake_get_stac(url, timeout, retries, retry_delay):
        calls.append(url)
        raise TimeoutError("read operation timed out")

    def fake_collection(*args, **kwargs):
        calls.append("collection-fallback")
        return ["bucket/relevant.parquet"]

    monkeypatch.setattr(http, "_http_get_stac", fake_get_stac)
    monkeypatch.setattr(http, "_http_resolve_stac_part_files_via_collection", fake_collection)

    keys = http._http_resolve_stac_part_files(
        "2026-08-19.0",
        "places",
        "place",
        (54.5, 37.5, 55.5, 38.5),
        retries=1,
        retry_delay=0,
    )

    assert keys == ["bucket/relevant.parquet"]
    assert calls[0] == "collection-fallback"


def test_download_progress_reports_speed_and_eta(monkeypatch, caplog):
    import overture.http as http

    progress = http._PartProgress("part.parquet", total_bytes=20 * 1024 * 1024, log_interval_s=0)
    progress._t0 = 0.0
    progress._last_log = 0.0
    progress._last_bytes = 0
    progress._bytes = 10 * 1024 * 1024
    monkeypatch.setattr(http.time, "monotonic", lambda: 2.0)

    with caplog.at_level("INFO", logger="wikiroutes.gis.overture"):
        progress.update(b"")

    assert "10.0/20.0 МБ" in caplog.text
    assert "скорость 5.00 МБ/с" in caplog.text
    assert "ETA 2.0 с" in caplog.text
    assert "средняя скорость 5.00 МБ/с" in progress.summary()

def test_stac_hosts_include_s3_catalog_mirror():
    import overture.http as http

    assert http._STAC_HTTP_HOSTS[0].endswith(".s3.us-west-2.amazonaws.com/stac")
    assert "https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/stac" in http._STAC_HTTP_HOSTS



def test_stac_accepts_url_list(monkeypatch):
    import overture.http as http

    calls = []

    def fake_fetch_chunk(url, start, timeout, chunk, retries, retry_delay):
        calls.append((url, start))
        return 206, b"x", 1

    monkeypatch.setattr(http, "_fetch_chunk", fake_fetch_chunk)
    monkeypatch.setattr(http, "_STAC_CHUNK_BYTES", 4)

    data = http._http_get_stac(
        ["https://a.example/stac.parquet", "https://b.example/stac.parquet"],
        timeout=1,
        retries=0,
        retry_delay=0,
    )

    assert data == b"x"
    assert calls == [(
        ["https://a.example/stac.parquet", "https://b.example/stac.parquet"],
        0,
    )]

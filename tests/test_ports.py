from overture.ports import CachePort, DirectionPort, RoutePort, StatsPort


class _Cache:
    def get(self, namespace, key):
        return None

    def put(self, namespace, key, value):
        return None


class _Direction:
    coords = ((0.0, 0.0), (1.0, 1.0))


class _Route:
    route_id = 1
    directions = (_Direction(),)
    error = False


class _Stats:
    total_area_m2 = 1.0
    corridor_m2 = 2.0
    count = 1
    ok = True


def test_ports_are_protocols_and_structurally_compatible():
    assert getattr(CachePort, "_is_protocol", False)
    assert getattr(DirectionPort, "_is_protocol", False)
    assert getattr(RoutePort, "_is_protocol", False)
    assert getattr(StatsPort, "_is_protocol", False)
    assert isinstance(_Cache(), CachePort)
    assert isinstance(_Direction(), DirectionPort)
    assert isinstance(_Route(), RoutePort)
    assert isinstance(_Stats(), StatsPort)

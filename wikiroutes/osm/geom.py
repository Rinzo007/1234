"""Сборка и склейка геометрий границ из OSM."""
from __future__ import annotations

from typing import Any

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry


def _parse_point(p: Any) -> tuple[float, float] | None:
    """Парсит точку из словаря {'lat':..., 'lon':...} или пары (lon, lat)."""
    if isinstance(p, dict):
        if "lat" in p and "lon" in p:
            return (float(p["lon"]), float(p["lat"]))
    elif isinstance(p, (list, tuple)) and len(p) >= 2:
        try:
            return (float(p[0]), float(p[1]))
        except (TypeError, ValueError):
            return None
    return None


def _region_level_relation(cache_key: str) -> int | None:
    return _REGION_LEVEL_RELATIONS.get(cache_key)


def _region_level_way(cache_key: str) -> int | None:
    return _REGION_LEVEL_WAYS.get(cache_key)


def _region_extra_relations(cache_key: str) -> tuple[int, ...]:
    """Дополнительные relation (муниципалитет, городской округ и т.п.)
    для склейки с границей города через unary_union.

    Заполняются в ``_REGION_EXTRA_RELATIONS``. Значение может быть либо
    кортежем ``(id, ...)``, либо одиночным int ``(id)`` — нормализуется к
    кортежу.
    """
    value = _REGION_EXTRA_RELATIONS.get(cache_key, ())
    if isinstance(value, int):
        return (value,)
    return tuple(value or ())


def _member_ring(member: dict[str, Any]) -> list[tuple[float, float]] | None:
    """Координаты way-участника relation в виде кольца или ``None``."""
    if member.get("type") != "way":
        return None
    coords = member.get("geometry")
    if not isinstance(coords, list):
        return None
    ring = [
        pt
        for p in coords
        if (_p := _parse_point(p)) is not None
        for pt in (_p,)
    ]
    return ring if len(ring) >= 2 else None


def _build_polygon(
    outer: list[tuple[float, float]],
    inner_rings: list[list[tuple[float, float]]],
) -> Polygon | None:
    """Полигон из внешнего кольца с внутренними дырами-кольцами."""
    if len(outer) < 4:
        return None
    shell = Polygon(outer)
    if not shell.is_valid:
        shell = make_valid(shell)
    holes = [r for r in inner_rings if shell.contains(Polygon(r))]
    return Polygon(outer, holes) if holes else Polygon(outer)


def _assemble_relation_geom(relation: dict[str, Any]) -> BaseGeometry | None:
    outers: list[list[tuple[float, float]]] = []
    inners: list[list[tuple[float, float]]] = []

    for member in relation.get("members", []):
        ring = _member_ring(member)
        if ring is None:
            continue
        if member.get("role") == "inner":
            inners.append(ring)
        else:
            outers.append(ring)

    inner_rings = _stitch_rings(inners)

    polygons: list[Polygon] = []
    for outer in _stitch_rings(outers):
        polygon = _build_polygon(outer, inner_rings)
        if polygon is not None:
            polygons.append(polygon)

    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def _assemble_way_geom(way: dict[str, Any]) -> BaseGeometry | None:
    """Собирает полигон из замкнутого way-элемента.

    Way (замкнутая линия) используется наравне с relation для городов,
    у которых граница не оформлена multipolygon-relation. Координаты берутся
    из ``geometry`` элемента ответа Overpass (``out geom``). Возвращает
    ``Polygon``/``MultiPolygon`` либо ``None`` для незамкнутой линии.
    """
    geoms: list[Polygon] = []
    ring = [
        pt
        for p in way.get("geometry", [])
        if (_p := _parse_point(p)) is not None
        for pt in (_p,)
    ]
    if len(ring) >= 4 and ring[0] == ring[-1]:
        polygon = _build_polygon(ring, [])
        if polygon is not None:
            geoms.append(polygon)

    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    return MultiPolygon(geoms)


def _try_merge_chain(
    chain: list[tuple[float, float]],
    seg: list[tuple[float, float]],
    eq: Any,
) -> bool:
    """Приклеивает сегмент к цепочке (по концам); True если успешно."""
    head, tail = chain[0], chain[-1]
    if eq(tail, seg[0]):
        chain.extend(seg[1:])
        return True
    if eq(tail, seg[-1]):
        chain.extend(reversed(seg[:-1]))
        return True
    if eq(head, seg[-1]):
        chain[:0] = seg[:-1]
        return True
    if eq(head, seg[0]):
        chain[:0] = list(reversed(seg[1:]))
        return True
    return False


def _extend_chain(
    chain: list[tuple[float, float]],
    unused: list[list[tuple[float, float]]],
    eq: Any,
) -> bool:
    """Приклеивает к цепочке первый подходящий сегмент; True при росте."""
    for i, seg in enumerate(unused):
        if not _try_merge_chain(chain, seg, eq):
            continue
        unused.pop(i)
        return True
    return False


def _stitch_rings(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    _EPS = 1e-6

    def _eq(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS

    unused = [list(seg) for seg in segments if len(seg) >= 2]
    rings: list[list[tuple[float, float]]] = []

    while unused:
        chain = list(unused.pop(0))
        while _extend_chain(chain, unused, _eq):
            pass
        if _eq(chain[0], chain[-1]) and len(chain) >= 4:
            rings.append(chain)

    return rings


_REGION_EXTRA_RELATIONS: dict[str, tuple[int, ...]] = {
    # Дополнительные relation-id муниципалитетов (городской округ и т.п.)
    # для склейки с границей города. Заполняется вручную:
    #   "city-slug": (relation_id, ...),
}


_REGION_LEVEL_RELATIONS: dict[str, int] = {
    "arhangelsk":1330715,
    "abakan":1474707,
    "almaty":2465058,
    "altmetievsk":3398094,
    "antracit":17967540,
    "anzhero-sudzhensk":1150979,
    "artemovsk":13630428,
    "arzamas":1582236,
    "astana":3087156,
    "astrakhan":1853865,
    "baku":2415335,
    "balashiha":3171497,
    "baranovichi":3629362,
    "barnaul":1550512,
    "batumi":2009237,
    "belaya-cerkov":11913383,
    "belgorod":7790937,
    "belgorod-dnestrovsky":13184690,
    "beltsy":58983,
    "bendery":9581354,
    "berdyansk":11857351,
    "berezniki":1574611,
    "berlin":62422,
    "bobrujsk":167857,
    "borisov":1749244,
    "brest":72615,
    "bucharest":377733,
    "cheboksary":1810276,
    "chelyabinsk":4442556,
    "cherkassy":11896205,
    "chernigov":12092168,
    "chernovtsy":12461821,
    "cheromushki":2169622,
    "daugavpils":13048683,
    "dimitrovgrad":2621608,
    "dnepropetrovsk":1017311,
    "donetsk":1413957,
    "druzhkovka":2900261,
    "dushanbe":4479735,
    "dzerzhynsk":13470215,
    "ekaterinburg":6564910,
    "elektrostal":1703097,
    "elets":3515980,
    "evpatoriya":2175059,
    "glazov":1186575,
    "gomel":163244,
    "gorlovka":3862199,
    "grodno":130921,
    "groznyj":1957640,
    "gyandzha":3764556,
    "gymree":7814501,
    "helsinki":34914,
    "herson":2175078,
    "hmelnitskiy":1792913,
    "hudzant":8795605,
    "irkutsk":1430613,
    "ivano-frankovsk":2362670,
    "joshkar-ola":2635526,
    "kaliningrad":1674442,
    "kaluga":1926754,
    "kamenets-podolskiy":2202756,
    "kamianske":2238215,
    "kamychin":1108341,
    "kansk":3511826,
    "kaunas":1067534,
    "kazan":3437391,
    "kemerovo":1312868,
    "kerch":2865045,
    "khabarovsk":2049848,
    "kharkov":3154746,
    "khimki":1703094,
    "kiev":421866,
    "kineshma":3442996,
    "kirovograd":2825228,
    "kiselevsk":1735835,
    "kishinew":1691801,
    "kislovodsk":12273850,
    "klaipeda":1014347,
    "kolomna":1703080,
    "kolpino":337423,
    "konstantinovka":3940146,
    "korolev":1703090,
    "kostroma":3536487,
    "kramatorsk":13630989,
    "krasnodar":7373058,
    "krasnoyarsk":1430616,
    "kremenchug":2320579,
    "krivoy-rog":1821193,
    "kursk":3348896,
    "kutaisi":2024547,
    "liepaya":13048685,
    "lipetsk":3134925,
    "lisichansk":3678529,
    "lugansk":2171253,
    "lutsk":1951964,
    "lvov":2032280,
    "lyubertsy":184002,
    "magnitogorsk":10185071,
    "mahachkala":4632505,
    "maikop":1850091,
    "makeevka":17963198,
    "mariupol":13285132,
    "melitopol":12287604,
    "mezhdurechensk":6729518,
    "michurinsk":5693608,
    "minsk":59195,
    "mogilev":62145,
    "mozyr":70815,
    "msk": 2555133,
    "murmansk":3374641,
    "murom":7236155,
    "mytischi":173406,
    "n-novgorod":1752948,
    "naberejnie-chelni":3437417,
    "nahodka":1836793,
    "nevinnomyssk":1030099,
    "nikolaev":11622860,
    "nikopol":2318332,
    "noginsk":179629,
    "novgorod":3396084,
    "novomoskovsk":1991064,
    "novorossiysk":1477110,
    "novoshahtinsk":1491892,
    "novosibirsk":366544,
    "novokuznetsk":1437127,
    "obninsk":3396088,
    "odessa":1413934,
    "odintsovo":1667827,
    "oktyabrski":10221652,
    "oleksandriya":2320570,
    "omsk":3442814,
    "orel":1775054,
    "orenburg":1398615,
    "orsha":178128,
    "osh":8496351,
    "panevezhis":968945,
    "pavlograd":1738040,
    "perm":3437213,
    "pervouralsk":17592059,
    "petrozavodsk":1701311,
    "petropavlovsk":2313542,
    "pinsk":1749248,
    "podolsk":181189,
    "poltava":12186978,
    "prague":435514,
    "prokopievsk":1735836,
    "riga":13048688,
    "rostov":2324453,
    "rostov-na-donu":1285772,
    "rovno":12234138,
    "rubcovsk":1442482,
    "rustavi":1997305,
    "ryazan":1782722,
    "samara":287507,
    "saransk":2307824,
    "sarapul":4788912,
    "saratov":3955288,
    "schelkovo":977573,
    "sergiev-posad":2458972,
    "sevastopol":3030976,
    "severodonetsk":4089182,
    "severodvinsk":2064358,
    "shahty":1482451,
    "shaulyai":968826,
    "simferopol":3826860,
    "slavyansk":13630809,
    "smolensk":3339993,
    "sochi":1430508,
    "sofia":1739543,
    "solikamsk":3437172,
    "spb":421007,
    "stary-oskol":2048253,
    "stavropol":6712557,
    "sukhumi":2027324,
    "sumy":3678531,
    "taganrog":1282148,
    "tallin":2164745,
    "tambov":4637058,
    "tartu":2153396,
    "tbilisi":1996871,
    "temirtau":20950774,
    "ternopol":12180839,
    "tiraspol":1702219,
    "tomsk":7261645,
    "tula":2538203,
    "tumen":2049854,
    "tver":930950,
    "ufa":1549169,
    "ukhta":1082932,
    "ulyanovsk":2049867,
    "uzhgorod":11842502,
    "velikie-luki":1306985,
    "vilnus":1529146,
    "vinnica":11914591,
    "vitebsk":6825777,
    "vladimir":389677,
    "vladivostok":1704857,
    "volgograd":3374767,
    "vologda":1327509,
    "vorkuta":9744962,
    "voronezh":3536492,
    "votkinsk":6821103,
    "warsaw":336075,
    "yaroslavl":1701435,
    "zaporozhye":1418311,
    "zelenograd":1320358,
    "zhitomir":2692156,
    "zhukovski":189473,
    "nalchik":8787138,
    "tashkent":2216724,
    "izhevsk":1670935,
    "engels":4869409,
    "pyatigorsk":2409550,
    "tolyatti":3338286,
    "london":175342,
    "magadan":7619051,
    "toretsk":2383991,
    "vyborg":1572051,
    "zlatoust":2295550,
    "ulianovsk":2049867,
    "belgrade":2728438,
}


_REGION_LEVEL_WAYS: dict[str, int] = {
    # Города, чья граница в OSM оформлена замкнутой линией way, а не
    # multipolygon-relation. Обрабатываются наравне с relation, но запрос
    # строится как ``way(id)`` вместо ``relation(id)``:
    #   "city-slug": way_id,
    "leninsk-kuznetsky":92753879,
    "pavlodar":170089891,
    "serpuhov":557235151,
    "turkestan":880812236,
    "ussuriysk":55668530,
    "kachkanar":31001611,
}

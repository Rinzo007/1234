from dataclasses import replace

from overture.config import OvertureConfig


def test_algorithm_signature_changes_when_algorithm_option_changes():
    base = OvertureConfig()
    changed = replace(base, line_simplify_m=1.0)
    assert base.algorithm_signature() != changed.algorithm_signature()


def test_cache_version_is_part_of_configuration():
    assert OvertureConfig().cache_version == 12
    assert OvertureConfig().algorithm_signature() != replace(
        OvertureConfig(), cache_version=13
    ).algorithm_signature()


def test_configuration_is_immutable():
    config = OvertureConfig()
    try:
        config.buffer_quad_segs = 8
    except Exception:
        pass
    else:
        raise AssertionError("OvertureConfig must be immutable")

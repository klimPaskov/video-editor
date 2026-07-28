from videoedit.pipeline.stage_key import make_stage_key


def test_stage_key_is_stable_for_mapping_order() -> None:
    first = make_stage_key("ingest", "1", ["b", "a"], {"x": 1, "y": 2})
    second = make_stage_key("ingest", "1", ["a", "b"], {"y": 2, "x": 1})
    assert first == second

from bait_mcp.config import deep_merge, load_config


def test_deep_merge_nested_override_leaves_base_untouched():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    over = {"a": {"y": 20}, "c": 4}
    merged = deep_merge(base, over)
    assert merged == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_load_config_defaults():
    cfg = load_config(None)
    assert cfg["mcp"]["host"] == "127.0.0.1"
    assert cfg["qserver"]["zmq_control_addr"] == "tcp://localhost:60615"
    assert cfg["qserver"]["user_group"] == "primary"


def test_load_config_yaml_override_preserves_siblings(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("qserver:\n  user_group: root\n")
    cfg = load_config(str(path))
    assert cfg["qserver"]["user_group"] == "root"
    # deep-merge keeps unspecified sibling defaults
    assert cfg["qserver"]["user"] == "bait_mcp"

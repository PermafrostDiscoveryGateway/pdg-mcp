from arcticdata_mcp.config import load_settings


def test_list_setting_accepts_toml_string_or_list(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[tools]\nexclude_tags = "low_level, node"\n')
    assert load_settings(p).tools.exclude_tags == ["low_level", "node"]
    p.write_text('[tools]\nexclude_tags = ["low_level"]\n')
    assert load_settings(p).tools.exclude_tags == ["low_level"]

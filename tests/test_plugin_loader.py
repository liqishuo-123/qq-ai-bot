from pathlib import Path

from src.plugin_loader import PluginRegistry


def test_plugin_specs_are_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "plugins.yaml"
    path.write_text(
        """
plugins:
  - name: demo
    module: demo_plugin
    dist: nonebot-plugin-demo
    enabled: true
    requires_python: ">=3.12"
""",
        encoding="utf-8",
    )
    specs = PluginRegistry._load_specs(path)
    assert len(specs) == 1
    assert specs[0].name == "demo"
    assert specs[0].module == "demo_plugin"
    assert specs[0].dist == "nonebot-plugin-demo"
    assert specs[0].enabled is True


def test_bad_plugin_yaml_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "plugins.yaml"
    path.write_text("plugins: [", encoding="utf-8")
    assert PluginRegistry._load_specs(path) == []


def test_plugin_yaml_root_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / "plugins.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert PluginRegistry._load_specs(path) == []

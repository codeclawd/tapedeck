from tapedeck import config


def test_env_file_fills_missing_settings_only(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("# comment\nTAPEDECK_LIBRARY=/srv/music\nTAPEDECK_PROXY = socks5://127.0.0.1:1080\n\nBROKEN LINE\n")
    monkeypatch.delenv("TAPEDECK_LIBRARY", raising=False)
    monkeypatch.setenv("TAPEDECK_PROXY", "http://already-set")
    config.load_env_file(f)
    import os
    assert os.environ["TAPEDECK_LIBRARY"] == "/srv/music"
    assert os.environ["TAPEDECK_PROXY"] == "http://already-set"


def test_js_runtime_prefers_deno_then_node(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda n: f"/bin/{n}" if n in ("deno", "node") else None)
    assert config.js_runtimes() == {"deno": {}}
    monkeypatch.setattr(config.shutil, "which", lambda n: "/usr/local/bin/node" if n == "node" else None)
    assert config.js_runtimes() == {"node": {"path": "/usr/local/bin/node"}}
    monkeypatch.setattr(config.shutil, "which", lambda n: None)
    assert config.js_runtimes() == {"deno": {}}


def test_env_file_strips_quotes_and_export(tmp_path, monkeypatch):
    import os
    f = tmp_path / "env"
    f.write_text('export TAPEDECK_X="/srv/music"\nTAPEDECK_Y=\'a b\'\n')
    monkeypatch.delenv("TAPEDECK_X", raising=False)
    monkeypatch.delenv("TAPEDECK_Y", raising=False)
    config.load_env_file(f)
    assert os.environ["TAPEDECK_X"] == "/srv/music" and os.environ["TAPEDECK_Y"] == "a b"


def test_library_path_is_absolute(monkeypatch):
    monkeypatch.setenv("TAPEDECK_LIBRARY", "rel/music")
    assert config.library().is_absolute()


def test_data_dir_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TAPEDECK_DATA", "~/td")
    assert config.data_dir() == tmp_path / "td"

import copy
import json
import os
import pickle

import pytest

import flask

# config keys used for the TestConfig
TEST_KEY = "foo"
SECRET_KEY = "config"


def common_object_test(app):
    assert app.secret_key == "config"
    assert app.config["TEST_KEY"] == "foo"
    assert "TestConfig" not in app.config


def test_config_from_pyfile():
    app = flask.Flask(__name__)
    app.config.from_pyfile(f"{__file__.rsplit('.', 1)[0]}.py")
    common_object_test(app)


def test_config_from_object():
    app = flask.Flask(__name__)
    app.config.from_object(__name__)
    common_object_test(app)


def test_config_from_file_json():
    app = flask.Flask(__name__)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    app.config.from_file(os.path.join(current_dir, "static", "config.json"), json.load)
    common_object_test(app)


def test_config_from_file_toml():
    tomllib = pytest.importorskip("tomllib", reason="tomllib added in 3.11")
    app = flask.Flask(__name__)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    app.config.from_file(
        os.path.join(current_dir, "static", "config.toml"), tomllib.load, text=False
    )
    common_object_test(app)


def test_from_prefixed_env(monkeypatch):
    monkeypatch.setenv("FLASK_STRING", "value")
    monkeypatch.setenv("FLASK_BOOL", "true")
    monkeypatch.setenv("FLASK_INT", "1")
    monkeypatch.setenv("FLASK_FLOAT", "1.2")
    monkeypatch.setenv("FLASK_LIST", "[1, 2]")
    monkeypatch.setenv("FLASK_DICT", '{"k": "v"}')
    monkeypatch.setenv("NOT_FLASK_OTHER", "other")

    app = flask.Flask(__name__)
    app.config.from_prefixed_env()

    assert app.config["STRING"] == "value"
    assert app.config["BOOL"] is True
    assert app.config["INT"] == 1
    assert app.config["FLOAT"] == 1.2
    assert app.config["LIST"] == [1, 2]
    assert app.config["DICT"] == {"k": "v"}
    assert "OTHER" not in app.config


def test_from_prefixed_env_custom_prefix(monkeypatch):
    monkeypatch.setenv("FLASK_A", "a")
    monkeypatch.setenv("NOT_FLASK_A", "b")

    app = flask.Flask(__name__)
    app.config.from_prefixed_env("NOT_FLASK")

    assert app.config["A"] == "b"


def test_from_prefixed_env_nested(monkeypatch):
    monkeypatch.setenv("FLASK_EXIST__ok", "other")
    monkeypatch.setenv("FLASK_EXIST__inner__ik", "2")
    monkeypatch.setenv("FLASK_EXIST__new__more", '{"k": false}')
    monkeypatch.setenv("FLASK_NEW__K", "v")

    app = flask.Flask(__name__)
    app.config["EXIST"] = {"ok": "value", "flag": True, "inner": {"ik": 1}}
    app.config.from_prefixed_env()

    if os.name != "nt":
        assert app.config["EXIST"] == {
            "ok": "other",
            "flag": True,
            "inner": {"ik": 2},
            "new": {"more": {"k": False}},
        }
    else:
        # Windows env var keys are always uppercase.
        assert app.config["EXIST"] == {
            "ok": "value",
            "OK": "other",
            "flag": True,
            "inner": {"ik": 1},
            "INNER": {"IK": 2},
            "NEW": {"MORE": {"k": False}},
        }

    assert app.config["NEW"] == {"K": "v"}


def test_config_from_mapping():
    app = flask.Flask(__name__)
    app.config.from_mapping({"SECRET_KEY": "config", "TEST_KEY": "foo"})
    common_object_test(app)

    app = flask.Flask(__name__)
    app.config.from_mapping([("SECRET_KEY", "config"), ("TEST_KEY", "foo")])
    common_object_test(app)

    app = flask.Flask(__name__)
    app.config.from_mapping(SECRET_KEY="config", TEST_KEY="foo")
    common_object_test(app)

    app = flask.Flask(__name__)
    app.config.from_mapping(SECRET_KEY="config", TEST_KEY="foo", skip_key="skip")
    common_object_test(app)

    app = flask.Flask(__name__)
    with pytest.raises(TypeError):
        app.config.from_mapping({}, {})


def test_config_from_class():
    class Base:
        TEST_KEY = "foo"

    class Test(Base):
        SECRET_KEY = "config"

    app = flask.Flask(__name__)
    app.config.from_object(Test)
    common_object_test(app)


def test_config_from_envvar(monkeypatch):
    monkeypatch.setattr("os.environ", {})
    app = flask.Flask(__name__)

    with pytest.raises(RuntimeError) as e:
        app.config.from_envvar("FOO_SETTINGS")

    assert "'FOO_SETTINGS' is not set" in str(e.value)
    assert not app.config.from_envvar("FOO_SETTINGS", silent=True)

    monkeypatch.setattr(
        "os.environ", {"FOO_SETTINGS": f"{__file__.rsplit('.', 1)[0]}.py"}
    )
    assert app.config.from_envvar("FOO_SETTINGS")
    common_object_test(app)


def test_config_from_envvar_missing(monkeypatch):
    monkeypatch.setattr("os.environ", {"FOO_SETTINGS": "missing.cfg"})
    app = flask.Flask(__name__)
    with pytest.raises(IOError) as e:
        app.config.from_envvar("FOO_SETTINGS")
    msg = str(e.value)
    assert msg.startswith(
        "[Errno 2] Unable to load configuration file (No such file or directory):"
    )
    assert msg.endswith("missing.cfg'")
    assert not app.config.from_envvar("FOO_SETTINGS", silent=True)


def test_config_missing():
    app = flask.Flask(__name__)
    with pytest.raises(IOError) as e:
        app.config.from_pyfile("missing.cfg")
    msg = str(e.value)
    assert msg.startswith(
        "[Errno 2] Unable to load configuration file (No such file or directory):"
    )
    assert msg.endswith("missing.cfg'")
    assert not app.config.from_pyfile("missing.cfg", silent=True)


def test_config_missing_file():
    app = flask.Flask(__name__)
    with pytest.raises(IOError) as e:
        app.config.from_file("missing.json", load=json.load)
    msg = str(e.value)
    assert msg.startswith(
        "[Errno 2] Unable to load configuration file (No such file or directory):"
    )
    assert msg.endswith("missing.json'")
    assert not app.config.from_file("missing.json", load=json.load, silent=True)


def test_custom_config_class():
    class Config(flask.Config):
        pass

    class Flask(flask.Flask):
        config_class = Config

    app = Flask(__name__)
    assert isinstance(app.config, Config)
    app.config.from_object(__name__)
    common_object_test(app)


def test_session_lifetime():
    app = flask.Flask(__name__)
    app.config["PERMANENT_SESSION_LIFETIME"] = 42
    assert app.permanent_session_lifetime.seconds == 42


def test_get_namespace():
    app = flask.Flask(__name__)
    app.config["FOO_OPTION_1"] = "foo option 1"
    app.config["FOO_OPTION_2"] = "foo option 2"
    app.config["BAR_STUFF_1"] = "bar stuff 1"
    app.config["BAR_STUFF_2"] = "bar stuff 2"
    foo_options = app.config.get_namespace("FOO_")
    assert 2 == len(foo_options)
    assert "foo option 1" == foo_options["option_1"]
    assert "foo option 2" == foo_options["option_2"]
    bar_options = app.config.get_namespace("BAR_", lowercase=False)
    assert 2 == len(bar_options)
    assert "bar stuff 1" == bar_options["STUFF_1"]
    assert "bar stuff 2" == bar_options["STUFF_2"]
    foo_options = app.config.get_namespace("FOO_", trim_namespace=False)
    assert 2 == len(foo_options)
    assert "foo option 1" == foo_options["foo_option_1"]
    assert "foo option 2" == foo_options["foo_option_2"]
    bar_options = app.config.get_namespace(
        "BAR_", lowercase=False, trim_namespace=False
    )
    assert 2 == len(bar_options)
    assert "bar stuff 1" == bar_options["BAR_STUFF_1"]
    assert "bar stuff 2" == bar_options["BAR_STUFF_2"]


@pytest.mark.parametrize("encoding", ["utf-8", "iso-8859-15", "latin-1"])
def test_from_pyfile_weird_encoding(tmp_path, encoding):
    f = tmp_path / "my_config.py"
    f.write_text(f'# -*- coding: {encoding} -*-\nTEST_VALUE = "föö"\n', encoding)
    app = flask.Flask(__name__)
    app.config.from_pyfile(os.fspath(f))
    value = app.config["TEST_VALUE"]
    assert value == "föö"


# ---------------------------------------------------------------------------
# Source tracking (get_source)
# ---------------------------------------------------------------------------


def test_get_source_default_and_direct(tmp_path):
    cfg = flask.Config(str(tmp_path), {"DEFAULT_KEY": "default"})

    source = cfg.get_source("DEFAULT_KEY")
    assert isinstance(source, flask.ConfigSource)
    assert source.method == "default"
    assert source.path is None
    assert source.name is None
    assert source.prefix is None

    # An unknown key has no source.
    assert cfg.get_source("MISSING") is None

    cfg["DIRECT_KEY"] = "direct"
    assert cfg.get_source("DIRECT_KEY").method == "direct"

    # Later assignment overrides both value and source.
    cfg["DEFAULT_KEY"] = "changed"
    assert cfg["DEFAULT_KEY"] == "changed"
    assert cfg.get_source("DEFAULT_KEY").method == "direct"


def test_get_source_pyfile(tmp_path):
    cfg = flask.Config(str(tmp_path))
    path = os.path.abspath(f"{__file__.rsplit('.', 1)[0]}.py")
    assert cfg.from_pyfile(path)

    source = cfg.get_source("SECRET_KEY")
    assert source.method == "pyfile"
    assert os.path.abspath(source.path) == path


def test_get_source_object_module():
    cfg = flask.Config(".")
    cfg.from_object(__name__)

    source = cfg.get_source("SECRET_KEY")
    assert source.method == "object"
    assert source.name == __name__


def test_get_source_object_class():
    class MyConfig:
        SECRET_KEY = "config"

    cfg = flask.Config(".")
    cfg.from_object(MyConfig)

    source = cfg.get_source("SECRET_KEY")
    assert source.method == "object"
    assert source.name == "MyConfig"


def test_get_source_envvar(monkeypatch, tmp_path):
    path = os.path.abspath(f"{__file__.rsplit('.', 1)[0]}.py")
    monkeypatch.setenv("FOO_SETTINGS", path)

    cfg = flask.Config(str(tmp_path))
    assert cfg.from_envvar("FOO_SETTINGS")

    source = cfg.get_source("SECRET_KEY")
    assert source.method == "envvar"
    assert source.name == "FOO_SETTINGS"
    assert os.path.abspath(source.path) == path


def test_get_source_file():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(current_dir, "static", "config.json")

    cfg = flask.Config(current_dir)
    assert cfg.from_file(path, load=json.load)

    source = cfg.get_source("TEST_KEY")
    assert source.method == "file"
    assert os.path.abspath(source.path) == os.path.abspath(path)


def test_get_source_mapping():
    cfg = flask.Config(".")
    cfg.from_mapping({"KEY": "a"}, OTHER="b")
    cfg.from_mapping([("LIST_KEY", "c")], lower="ignored")

    assert cfg.get_source("KEY").method == "mapping"
    assert cfg.get_source("OTHER").method == "mapping"
    assert cfg.get_source("LIST_KEY").method == "mapping"
    assert cfg.get_source("lower") is None


def test_get_source_prefixed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_STRING", "value")
    monkeypatch.setenv("FLASK_BAD", "not valid json {")

    cfg = flask.Config(str(tmp_path))
    cfg.from_prefixed_env()

    source = cfg.get_source("STRING")
    assert source.method == "prefixed_env"
    assert source.prefix == "FLASK"
    assert source.name == "FLASK_STRING"

    # A value that fails JSON parsing stays a string but is still tracked.
    bad_source = cfg.get_source("BAD")
    assert bad_source.method == "prefixed_env"
    assert bad_source.name == "FLASK_BAD"
    assert cfg["BAD"] == "not valid json {"


def test_get_source_prefixed_env_custom_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAPP_A", "a")

    cfg = flask.Config(str(tmp_path))
    cfg.from_prefixed_env("MYAPP")

    source = cfg.get_source("A")
    assert source.method == "prefixed_env"
    assert source.prefix == "MYAPP"
    assert source.name == "MYAPP_A"


def test_get_source_prefixed_env_nested(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_EXIST__OK", "other")
    monkeypatch.setenv("FLASK_NEW__K", "v")

    cfg = flask.Config(str(tmp_path))
    cfg["EXIST"] = {"ok": "value"}
    cfg.from_prefixed_env()

    # The existing intermediate key keeps its previous source; nested
    # loading only attributes the leaf key.
    assert cfg.get_source("EXIST").method == "direct"

    # An auto-created intermediate key has no source (only the leaf is
    # attributed), even though it exists in the config.
    assert "NEW" in cfg
    assert cfg["NEW"]["K"] == "v"
    assert cfg.get_source("NEW") is None


def test_get_source_last_write_wins(tmp_path):
    cfg = flask.Config(str(tmp_path), {"KEY": "default"})
    assert cfg.get_source("KEY").method == "default"

    cfg.from_mapping({"KEY": "mapping"})
    assert cfg.get_source("KEY").method == "mapping"
    assert cfg["KEY"] == "mapping"

    cfg["KEY"] = "direct"
    assert cfg.get_source("KEY").method == "direct"
    assert cfg["KEY"] == "direct"


def test_silent_failures_record_no_sources(monkeypatch, tmp_path):
    cfg = flask.Config(str(tmp_path))

    assert not cfg.from_pyfile("missing.cfg", silent=True)
    assert dict(cfg) == {}

    monkeypatch.delenv("NOPE_SETTINGS", raising=False)
    assert not cfg.from_envvar("NOPE_SETTINGS", silent=True)
    assert dict(cfg) == {}

    monkeypatch.setenv("MISSING_SETTINGS", "missing.cfg")
    assert not cfg.from_envvar("MISSING_SETTINGS", silent=True)
    assert dict(cfg) == {}

    assert not cfg.from_file("missing.json", load=json.load, silent=True)
    assert dict(cfg) == {}


def test_source_removed_when_key_removed(tmp_path):
    cfg = flask.Config(str(tmp_path), {"A": 1})
    cfg["B"] = 2
    cfg["C"] = 3
    cfg["D"] = 4

    del cfg["A"]
    assert "A" not in cfg
    assert cfg.get_source("A") is None

    assert cfg.pop("B") == 2
    assert cfg.get_source("B") is None
    assert cfg.pop("B", "default") == "default"

    key, _ = cfg.popitem()
    assert cfg.get_source(key) is None

    cfg.clear()
    assert dict(cfg) == {}
    assert cfg.get_source("D") is None


def test_dict_mutation_methods_record_direct(tmp_path):
    cfg = flask.Config(str(tmp_path), {"A": 1})

    cfg.update({"B": 2}, C=3)
    assert cfg.get_source("B").method == "direct"
    assert cfg.get_source("C").method == "direct"

    assert cfg.setdefault("A", 9) == 1
    assert cfg.get_source("A").method == "default"
    assert cfg.setdefault("D", 4) == 4
    assert cfg.get_source("D").method == "direct"


def test_repr_unchanged(tmp_path):
    cfg = flask.Config(str(tmp_path), {"A": 1})
    cfg["B"] = 2
    assert repr(cfg) == f"<Config {dict.__repr__(cfg)}>"
    assert repr(cfg) == "<Config {'A': 1, 'B': 2}>"


def test_pickle_roundtrip(tmp_path):
    cfg = flask.Config(str(tmp_path), {"A": 1})
    cfg["B"] = 2
    cfg.from_mapping({"C": 3})

    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(cfg, protocol))

        assert dict(restored) == {"A": 1, "B": 2, "C": 3}
        assert restored.root_path == str(tmp_path)
        assert repr(restored) == repr(cfg)
        assert restored.get_source("A").method == "default"
        assert restored.get_source("B").method == "direct"
        assert restored.get_source("C").method == "mapping"
        assert restored.get_source("MISSING") is None


def test_deepcopy_preserves_sources(tmp_path):
    cfg = flask.Config(str(tmp_path), {"A": [1]})
    cfg["B"] = [2]

    restored = copy.deepcopy(cfg)
    assert type(restored) is flask.Config
    assert restored == cfg
    assert restored is not cfg
    assert restored["A"] is not cfg["A"]
    assert restored.root_path == str(tmp_path)
    assert restored.get_source("A").method == "default"
    assert restored.get_source("B").method == "direct"


def test_app_default_config_sources():
    app = flask.Flask(__name__)
    source = app.config.get_source("DEBUG")
    assert source is not None
    assert source.method == "default"

    app.config["CUSTOM"] = True
    assert app.config.get_source("CUSTOM").method == "direct"

"""Tests for the GUI's tkinter-free layers: settings persistence + paths."""

from pathlib import Path

from kalshi_bot.gui_settings import (FIELDS, GROUPS, defaults, export_to_environ,
                                     initial_values, load_settings,
                                     normalize_bool, save_settings)
from kalshi_bot import paths


def test_every_field_is_grouped_and_unique():
    keys = [f.key for f in FIELDS]
    assert len(keys) == len(set(keys))
    assert all(f.group in GROUPS for f in FIELDS)
    assert set(defaults()) == set(keys)


def test_save_load_round_trip(tmp_path):
    target = tmp_path / "nested" / "settings.env"
    values = {"KALSHI_ENV": "demo", "ARB_BUDGET_FRAC": "0.10",
              "KALSHI_API_KEY_ID": "abc-123", "EMPTY": ""}
    save_settings(target, values)
    loaded = load_settings(target)
    assert loaded["KALSHI_ENV"] == "demo"
    assert loaded["ARB_BUDGET_FRAC"] == "0.10"
    assert "EMPTY" not in loaded                  # empties not persisted
    assert load_settings(tmp_path / "missing.env") == {}


def test_load_ignores_comments_and_junk(tmp_path):
    target = tmp_path / "s.env"
    target.write_text("# comment\n\nKALSHI_ENV = prod\nnot a pair\nX=1=2\n")
    loaded = load_settings(target)
    assert loaded["KALSHI_ENV"] == "prod"
    assert loaded["X"] == "1=2"


def test_initial_values_precedence():
    file_values = {"KALSHI_ENV": "prod", "ARB_BUDGET_FRAC": "0.11"}
    env = {"KALSHI_ENV": "demo"}                  # explicit env wins
    values = initial_values(file_values, environ=env)
    assert values["KALSHI_ENV"] == "demo"
    assert values["ARB_BUDGET_FRAC"] == "0.11"    # file beats default
    assert values["KELLY_FRACTION"] == "0.25"     # default fallback


def test_export_is_authoritative():
    env = {"ARB_BUDGET_FRAC": "0.30", "UNRELATED": "keep"}
    export_to_environ({"ARB_BUDGET_FRAC": "0.10", "KALSHI_ENV": "demo",
                       "KALSHI_API_KEY_ID": ""}, environ=env)
    assert env["ARB_BUDGET_FRAC"] == "0.10"       # stale value replaced
    assert env["KALSHI_ENV"] == "demo"
    assert "KALSHI_API_KEY_ID" not in env         # empty removes the key
    assert env["UNRELATED"] == "keep"             # unmanaged keys untouched


def test_normalize_bool():
    assert normalize_bool("TRUE") and normalize_bool("1") and normalize_bool("on")
    assert not normalize_bool("false") and not normalize_bool("")


def test_paths_are_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    base = paths.user_data_dir()
    assert base == Path(tmp_path) / "kalshi-bot"
    assert paths.settings_path().parent == base
    assert paths.default_db_path().parent == base
    created = paths.ensure_data_dir()
    assert created.is_dir()


def test_icon_generator(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_icon", Path(__file__).parent.parent / "assets" / "make_icon.py")
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)
    data = make_icon.build_ico()
    # ICONDIR: reserved=0, type=1 (icon), count=1; entry says 32bpp
    assert data[:6] == b"\x00\x00\x01\x00\x01\x00"
    assert len(data) > 22 + 40 + 64 * 64 * 4

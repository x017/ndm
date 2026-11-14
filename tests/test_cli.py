from __future__ import annotations

import inspect

import pytest

from ndm import cli
from ndm.desktop import Desktop


def test_desktop_has_no_screenshot_parameter_or_flag(monkeypatch, capsys, tmp_path):
    calls = []

    class FakeDesktop:
        def __init__(self, *, data_dir):
            calls.append(data_dir)

        def run(self):
            calls.append("run")

    monkeypatch.setattr("ndm.desktop.Desktop", FakeDesktop)
    cli.main(["desktop", "--data-dir", str(tmp_path)])
    assert calls == [tmp_path.resolve(), "run"]
    with pytest.raises(SystemExit) as exc:
        cli.main(["desktop", "--screenshot", "out.png"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --screenshot" in capsys.readouterr().err
    assert "screenshot" not in inspect.signature(Desktop).parameters


def test_bare_ndm_opens_desktop_without_optional_args(monkeypatch, tmp_path):
    calls = []

    class FakeDesktop:
        def __init__(self, *, data_dir):
            calls.append(data_dir)

        def run(self):
            calls.append("run")

    monkeypatch.setattr("ndm.desktop.Desktop", FakeDesktop)
    monkeypatch.setenv("NDM_HOME", str(tmp_path))
    cli.main([])
    assert calls == [tmp_path.resolve(), "run"]

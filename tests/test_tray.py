"""Tray supervision without requiring a display server or tray backend."""
from unittest.mock import Mock, patch

from ndm.tray import TrayController


def test_open_desktop_reuses_running_child(tmp_path):
    controller = TrayController(tmp_path)
    child = Mock()
    child.poll.return_value = None
    with patch('ndm.tray.subprocess.Popen', return_value=child) as spawn:
        controller.open_desktop()
        controller.open_desktop()
        assert spawn.call_count == 1
        command = spawn.call_args.args[0]
        assert command[1:] == ['-m', 'ndm', 'desktop', '--data-dir', str(tmp_path)]
        child.poll.return_value = 0
        controller.open_desktop()
        assert spawn.call_count == 2


def test_web_does_not_start_listener(tmp_path):
    with patch('ndm.tray.webbrowser.open') as browser, patch('ndm.tray.subprocess.Popen') as spawn:
        TrayController(tmp_path).open_web()
        browser.assert_called_once_with('http://127.0.0.1:8765')
        spawn.assert_not_called()

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


def test_tray_starts_daemon_without_desktop(tmp_path):
    controller = TrayController(tmp_path)
    with patch('ndm.tray.is_running', side_effect=[False, True]), patch('ndm.tray.start_detached') as start, patch('ndm.tray.subprocess.Popen') as desktop:
        controller.ensure_daemon()
        start.assert_called_once_with(tmp_path)
        desktop.assert_not_called()


def test_existing_daemon_is_reused(tmp_path):
    with patch('ndm.tray.is_running', return_value=True), patch('ndm.tray.start_detached') as start:
        TrayController(tmp_path).ensure_daemon()
        start.assert_not_called()


def test_pause_resume_and_live_status(tmp_path):
    controller = TrayController(tmp_path)
    state = {'stats': {'active': 2, 'queued': 4}, 'settings': {'engine_running': True}}
    with patch.object(controller, 'ensure_daemon'), patch('ndm.tray.request', return_value=state) as call:
        controller.set_running(False)
        call.assert_called_with(tmp_path, 'settings', {'engine_running': False}, timeout=5)
        controller.set_running(True)
        call.assert_called_with(tmp_path, 'settings', {'engine_running': True}, timeout=5)
        assert controller.status() == 'NDM — Running: 2 active, 4 queued'
        state['settings']['engine_running'] = False
        assert 'Paused' in controller.status()

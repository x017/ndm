"""Setup planning is testable without root, package installation or a display."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ndm_setup_wizard', Path(__file__).parents[1] / 'setup.py')
wizard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wizard
spec.loader.exec_module(wizard)


def test_dry_run_has_no_side_effects(tmp_path):
    settings = wizard.Settings('user', 'tray', True, tmp_path)
    with patch.object(wizard.subprocess, 'run') as run, patch.object(wizard.subprocess, 'Popen') as spawn:
        installer = wizard.Installer(settings, dry_run=True)
        installer.install()
        installer.configure()
    run.assert_not_called()
    spawn.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_system_install_only_elevates_installation(tmp_path):
    settings = wizard.Settings('system', 'daemon', False, tmp_path)
    with patch.object(wizard.subprocess, 'run') as run:
        installer = wizard.Installer(settings)
        installer.install()
        installer.configure()
    commands = [c.args[0] for c in run.call_args_list]
    assert commands[0][:2] == ['sudo', 'bash']
    assert commands[1] == ['/usr/local/bin/ndm', 'service', 'install', '--start']


def test_autostart_is_per_user(tmp_path):
    settings = wizard.Settings('system', 'tray', False, tmp_path)
    wizard.Installer(settings).configure()
    entry = (tmp_path / '.config/autostart/ndm.desktop').read_text()
    assert 'Exec="/usr/local/bin/ndm" tray --no-window' in entry
    assert 'sudo' not in entry


def test_menu_input_validation():
    with patch('builtins.input', side_effect=['bad', '0', '2']):
        assert wizard.choose('Scope', [('user', 'User'), ('system', 'System')]) == 'system'
    with patch('builtins.input', return_value=''):
        assert wizard.confirm('Continue?') is False


def test_desktop_entry_quotes_special_paths():
    entry = wizard.desktop_entry(Path('/home/a b/100%/ndm'))
    assert 'Exec="/home/a b/100%%/ndm" desktop' in entry

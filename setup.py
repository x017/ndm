#!/usr/bin/env python3
"""Interactive Linux installer, not a setuptools build script.

Packaging remains defined by pyproject.toml. Run: python3 setup.py
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def style(text, code="35;1"):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def heading(title, subtitle=""):
    print("\n" + style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
    print("  " + style(title))
    if subtitle:
        print("  " + subtitle)
    print(style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))


def choose(title, options, default=1):
    print("\n  " + style(title, "1"))
    for i, (_, label) in enumerate(options, 1):
        mark = "  recommended" if i == default else ""
        print(f"    {style(str(i), '36')}  {label}{style(mark, '2')}")
    while True:
        value = input(f"\n  Select [1–{len(options)}] ({default}): ").strip() or str(default)
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1][0]
        print("  Please enter one of the numbers above.")


def confirm(prompt, default=False):
    while True:
        value = input(f"  {prompt} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "n", "no"}:
            return value in {"y", "yes"}
        print("  Please enter yes or no.")


@dataclass
class Settings:
    scope: str
    startup: str
    launch: bool
    home: Path

    @property
    def command(self):
        return Path('/usr/local/bin/ndm') if self.scope == 'system' else self.home / '.local/bin/ndm'


def prerequisites():
    try:
        release = Path('/etc/os-release').read_text().lower()
    except OSError:
        release = ''
    if 'arch' in release or 'manjaro' in release:
        return 'sudo pacman -S --needed python python-pip ffmpeg python-gobject python-cairo libayatana-appindicator'
    if 'fedora' in release:
        return 'sudo dnf install python3 python3-pip ffmpeg python3-gobject libayatana-appindicator-gtk3'
    return 'sudo apt install python3-venv python3-gi python3-gi-cairo ffmpeg libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1'


class Installer:
    def __init__(self, settings, dry_run=False):
        self.settings = settings
        self.dry_run = dry_run

    def run(self, args):
        print("  " + style("› ", "36") + shlex.join(str(a) for a in args))
        if not self.dry_run:
            subprocess.run([str(a) for a in args], check=True)

    def write(self, path, content, executable=False):
        print(f"  {'Would write' if self.dry_run else 'Writing'} {path}")
        if self.dry_run:
            return
        if path.exists() and not confirm(f"Replace {path}?", default=False):
            raise RuntimeError("Cancelled rather than replacing an existing file.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            path.chmod(0o755)

    def install(self):
        s = self.settings
        if s.scope == 'system':
            # Only the system installation step is elevated; user configuration
            # and application launches always run as the invoking normal user.
            self.run(['sudo', 'bash', ROOT / 'scripts/install-system.sh'])
        else:
            env = s.home / '.local/share/ndm-app'
            if env.exists():
                raise RuntimeError(f'{env} already exists. Keep it or remove it explicitly before reinstalling.')
            self.run([sys.executable, '-m', 'venv', '--system-site-packages', env])
            self.run([env / 'bin/python', '-m', 'pip', 'install', f'{ROOT}[tray]'])
            self.write(s.command, '#!/bin/sh\nexec ' + shlex.quote(str(env / 'bin/ndm')) + ' "$@"\n', executable=True)
            icon = s.home / '.local/share/icons/hicolor/scalable/apps/ndm.svg'
            self.write(icon, (ROOT / 'packaging/linux/ndm.svg').read_text())
            self.write(s.home / '.local/share/applications/io.github.x017.ndm.desktop', desktop_entry(s.command, tray=True))

    def configure(self):
        s = self.settings
        if s.startup == 'tray':
            self.write(s.home / '.config/autostart/ndm.desktop', desktop_entry(s.command, tray=True, no_window=True))
        elif s.startup == 'daemon':
            self.run([s.command, 'service', 'install', '--start'])
        if s.launch:
            if self.dry_run:
                print(f'  Would launch {s.command} tray')
            else:
                subprocess.Popen([str(s.command), 'tray'], start_new_session=True)


def desktop_entry(command, *, tray=False, no_window=False):
    # Desktop Exec syntax is not shell syntax: double-quote and escape reserved
    # characters; double percent signs to avoid accidental field-code expansion.
    executable = str(command).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    args = 'tray' if tray else 'desktop'
    if no_window:
        args += ' --no-window'
    return f'[Desktop Entry]\nType=Application\nName=NDM\nComment=Download manager\nExec="{executable}" {args}\nIcon=ndm\nTerminal=false\nCategories=Network;FileTransfer;\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='NDM interactive Linux setup wizard')
    parser.add_argument('--dry-run', action='store_true', help='Preview commands and files without changing anything')
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux'):
        parser.error('This wizard currently supports Linux only.')
    if os.geteuid() == 0:
        parser.error('Run python3 setup.py as your normal user, NOT with sudo. Elevation is requested only when needed.')
    if not sys.stdin.isatty():
        parser.error('Use an interactive terminal to run this wizard.')
    heading('NDM  /  Setup', 'Your downloads. Your desktop. One shared engine.')
    print('  Linux installer · Python 3.11–3.15 · No settings changed yet.')
    print('\n  Install these system prerequisites first if needed:\n\n  ' + prerequisites())
    print('\n  GNOME also needs an enabled AppIndicator extension for tray icons.')
    scope = choose('01 / Installation', [('user', 'Only me — no administrator privileges'), ('system', 'All users — /opt/ndm and /usr/local/bin (sudo)')])
    startup = choose('02 / Start at sign-in', [('none', 'Do not start automatically'), ('tray', 'Tray only — start the engine, keep the window closed'), ('daemon', 'Background engine only — systemd user service')])
    print('\n  Startup applies only to your account, even for an all-user install.')
    print('  Existing startup entries/services are not removed automatically.')
    launch = confirm('Open NDM with its tray after setup?', default=True)
    s = Settings(scope, startup, launch, Path.home())
    heading('Review your installation', 'Dry run — no files will change.' if args.dry_run else 'Nothing runs until you confirm.')
    print(f'  Install scope   {s.scope}\n  Command         {s.command}\n  Startup         {s.startup}\n  Launch now      {s.launch}')
    print('  Downloads and settings stay in your user account.\n')
    if not confirm('Continue?', default=False):
        print('\n  Setup cancelled. No changes made.')
        return
    installer = Installer(s, args.dry_run)
    heading('Installing NDM')
    installer.install()
    heading('Configuring your session')
    installer.configure()
    heading('Plan complete' if args.dry_run else 'NDM is ready')
    print(f'  Desktop + tray: {s.command} tray\n  Web interface:  {s.command} web')
    if s.scope == 'user' and str(s.command.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('  Add ~/.local/bin to your PATH to use the short ndm command.')
    print('  The daemon keeps downloads running when the window closes.\n')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n  Setup interrupted. Completed installation steps are not rolled back.')
        sys.exit(130)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'\n  Setup could not finish: {exc}', file=sys.stderr)
        print('  Completed steps are kept. Review the output before retrying.', file=sys.stderr)
        sys.exit(1)

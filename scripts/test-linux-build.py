"""Exercise the Linux build/deploy launcher with fake tools and services.

Run: python scripts/test-linux-build.py
No installed service, network or database is modified.
"""
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BuildLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'launch').mkdir()
        for name in ('build.sh', 'common.sh'):
            shutil.copyfile(ROOT / 'launch' / name, self.root / 'launch' / name)
        (self.root / 'node_modules').mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls'
        home = self.root / 'home'
        units = home / '.config/systemd/user'
        units.mkdir(parents=True)
        for name in ('api', 'worker', 'migrate', 'postgresql', 'redis'):
            (units / f'narrify-{name}.service').touch()
        runtime = self.root / 'runtime'
        runtime.mkdir()
        bus = socket.socket(socket.AF_UNIX)
        bus.bind(str(runtime / 'bus'))
        self.addCleanup(bus.close)
        self.env = {**os.environ, 'HOME': str(home), 'XDG_RUNTIME_DIR': str(runtime),
                    'PATH': f'{self.bin}:{os.environ["PATH"]}', 'CALL_LOG': str(self.log)}
        self.tool('npm', '[[ "${FAIL_BUILD:-}" != 1 || "$*" != "run build:all" ]]')
        self.tool('curl', 'exit 0')
        self.tool('systemctl', '''
if [[ "$*" == *"--property=LoadState"* ]]; then
  echo "${LOAD_STATE:-loaded}"
fi
if [[ "${FAIL_MIGRATION:-}" == 1 && "$*" == *"restart narrify-migrate.service"* ]]; then
  exit 1
fi
exit 0
''')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/usr/bin/env bash\nset -e\nprintf "python %s\\n" "$*" >> "$CALL_LOG"\n')
        python.chmod(0o755)

    def tool(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/usr/bin/env bash\nset -e\nprintf "{name} %s\\n" "$*" >> "$CALL_LOG"\n{body}\n')
        path.chmod(0o755)

    def run_build(self, *args, **env):
        result = subprocess.run(['bash', str(self.root / 'launch/build.sh'), *args],
                                env={**self.env, **env}, capture_output=True, text=True)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_default_builds_before_stop_then_migrates_and_starts(self):
        result, calls = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ['npm run build:all', 'systemctl --user daemon-reload',
                    'systemctl --user stop narrify-api.service narrify-worker.service',
                    'systemctl --user restart narrify-migrate.service',
                    'systemctl --user start narrify-api.service narrify-worker.service']
        positions = [calls.index(call) for call in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(any(call.startswith('curl ') for call in calls))

    def test_failed_build_does_not_stop_services(self):
        result, calls = self.run_build(FAIL_BUILD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(' stop ' in call or ' restart ' in call for call in calls))

    def test_failed_migration_does_not_start_application(self):
        result, calls = self.run_build(FAIL_MIGRATION='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemctl --user stop narrify-api.service narrify-worker.service', calls)
        self.assertNotIn('systemctl --user start narrify-api.service narrify-worker.service', calls)

    def test_build_only_never_calls_systemd(self):
        result, calls = self.run_build('--build-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['npm run build:all'])

    def test_dependency_install_stops_before_installing(self):
        result, calls = self.run_build('--install-deps')
        self.assertEqual(result.returncode, 0, result.stderr)
        stop = calls.index('systemctl --user stop narrify-api.service narrify-worker.service')
        self.assertLess(stop, calls.index('npm ci'))
        self.assertLess(calls.index('python -m pip check'), calls.index('npm run build:all'))

    def test_missing_service_fails_before_build(self):
        result, calls = self.run_build(LOAD_STATE='not-found')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('npm run build:all', calls)

    def test_build_only_rejects_dependency_updates(self):
        result, calls = self.run_build('--build-only', '--install-deps')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_held_launcher_lock_prevents_build(self):
        import fcntl
        folder = self.root / '.narrify'
        folder.mkdir()
        with (folder / 'dev-launch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, calls = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()

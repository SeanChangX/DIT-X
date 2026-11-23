"""CLI behaviour: help, error messages, global flags, JSON, confirmations, init, completion.

python3 -m unittest discover -s tools/tests -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

# tests must not depend on the caller's settings (DIT_REPOS_FILE, ROS_DISTRO, ...)
for _k in [k for k in os.environ if k.startswith(('DIT_', 'DITX_')) or k in ('ROS_DISTRO', 'CI')]:
    del os.environ[_k]

from ditx import cli, ui  # noqa: E402
from ditx.complete import complete  # noqa: E402
from ditx.workspace import Workspace  # noqa: E402


def make_ws(domains=('main', 'localization')):
    root = tempfile.mkdtemp(prefix='ditx-cli-test-')
    os.makedirs(os.path.join(root, 'tools', 'ditx'))
    shutil.copytree(os.path.join(ROOT, 'config'), os.path.join(root, 'config'))
    with open(os.path.join(root, 'ditx.repos'), 'w') as f:
        f.write('repositories:\n')
        for d in domains:
            f.write(
                f'  domains/{d}:\n    type: git\n    url: https://example.com/{d}.git\n'
                f'    version: develop\n'
            )
    for d in domains:
        for pkg in (f'{d}_bringup', f'{d}_core'):
            p = os.path.join(root, 'domains', d, 'src', pkg)
            os.makedirs(os.path.join(p, 'launch'))
            with open(os.path.join(p, 'package.xml'), 'w') as f:
                f.write(f'<package><name>{pkg}</name></package>')
    return Workspace(root)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.ws = make_ws()
        self.saved = (ui.DRY_RUN, ui.QUIET, ui.VERBOSE, cli.LOG)

    def tearDown(self):
        ui.DRY_RUN, ui.QUIET, ui.VERBOSE, cli.LOG = self.saved
        shutil.rmtree(self.ws.root)

    def run_cli(self, *argv, stdin_tty=False):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(cli, 'find_root', return_value=self.ws.root),
            mock.patch.object(sys.stdin, 'isatty', return_value=stdin_tty),
        ):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()


class HelpTest(CliTestCase):
    def test_top_help_is_grouped(self):
        rc, out, _ = self.run_cli('--help')
        self.assertEqual(rc, 0)
        for section in (
            'Develop commands',
            'Robot commands',
            'Workspace commands',
            'Global flags',
            'Examples',
            'ditx help <command>',
        ):
            self.assertIn(section, out)
        self.assertNotIn('__complete', out)

    def test_help_command_equals_dash_dash_help(self):
        rc, out, _ = self.run_cli('help', 'build')
        self.assertEqual(rc, 0)
        self.assertIn('usage: ditx build', out)
        self.assertIn('Examples:', out)
        self.assertIn('-d DOMAIN...', out)
        self.assertEqual(self.run_cli('build', '--help')[1], out)

    def test_version(self):
        rc, out, _ = self.run_cli('-V')
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith('ditx '))


class ErrorTest(CliTestCase):
    def test_command_typo(self):
        rc, _, err = self.run_cli('biuld')
        self.assertEqual(rc, 2)
        self.assertIn("did you mean 'build'", err)

    def test_flag_typo_names_the_command_and_the_flag(self):
        rc, _, err = self.run_cli('build', '--domian', 'x')
        self.assertEqual(rc, 2)
        self.assertIn('ditx build: unknown flag --domian; did you mean --domain?', err)
        self.assertIn('see: ditx build --help', err)

    def test_global_flags_before_the_command(self):
        rc, _, err = self.run_cli('-v', 'topcs')
        self.assertEqual(rc, 2)
        self.assertIn("unknown command 'topcs'", err)

    def test_unknown_robot_in_doctor(self):
        rc, _, err = self.run_cli('doctor', '-r', 'nope', '--json')
        self.assertEqual(rc, 1)
        self.assertIn("unknown robot 'nope'", err)


class JsonTest(CliTestCase):
    def test_list_json_is_pure_json(self):
        rc, out, _ = self.run_cli('list', '--json')
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertIn(
            {
                'domain': 'main',
                'package': 'main_core',
                'type': 'python',
                'path': 'domains/main/src/main_core',
            },
            rows,
        )

    def test_status_json(self):
        rc, out, _ = self.run_cli('status', '--no-fetch', '--json')
        self.assertEqual(rc, 0)
        repos = {r['repo']: r for r in json.loads(out)}
        self.assertIn('(shell)', repos)
        self.assertFalse(repos['main']['synced'])  # a plain directory, not a git clone

    def test_plugins_json_empty(self):
        rc, out, _ = self.run_cli('plugins', '--json')
        self.assertEqual(rc, 0)
        self.assertIsInstance(json.loads(out), list)


class ConfirmTest(CliTestCase):
    def test_destructive_needs_yes_without_a_terminal(self):
        rc, _, err = self.run_cli('clean', '--all')
        self.assertEqual(rc, 2)
        self.assertIn('--yes', err)

    def test_yes_and_tui_confirmation_skip_the_question(self):
        self.assertEqual(self.run_cli('clean', '--all', '-y')[0], 0)
        with mock.patch.dict(os.environ, {'DITX_CONFIRMED': '1'}):
            self.assertEqual(self.run_cli('clean', '--all')[0], 0)

    def test_dry_run_does_not_ask(self):
        self.assertEqual(self.run_cli('-n', 'clean', '--all')[0], 0)

    def test_answer_no_cancels(self):
        with mock.patch('builtins.input', return_value='n'):
            rc, _, err = self.run_cli('clean', '--all', stdin_tty=True)
        self.assertEqual(rc, 1)
        self.assertIn('cancelled', err)


class InitTest(CliTestCase):
    def test_refuses_without_a_terminal(self):
        rc, _, err = self.run_cli('init')
        self.assertEqual(rc, 1)
        self.assertIn('--defaults', err)

    def test_defaults_keep_comments_and_unknown_lines(self):
        env = os.path.join(self.ws.root, '.env')
        with open(env, 'w') as f:
            f.write('# mine\nFOO=1\nROS_DISTRO=humble\n')
        rc, _, _ = self.run_cli('init', '--defaults')
        self.assertEqual(rc, 0)
        with open(env) as f:
            text = f.read()
        lines = text.splitlines()
        for line in (
            '# mine',
            'FOO=1',
            'ROS_DISTRO=humble',
            'DIT_ROBOT=sim',
            'DIT_ROBOT_USER=main',
        ):
            self.assertIn(line, lines)


class DryRunTaskTest(unittest.TestCase):
    def test_dry_run_never_claims_success(self):
        err = io.StringIO()
        saved = ui.DRY_RUN
        ui.DRY_RUN = True
        try:
            with contextlib.redirect_stderr(err):
                ui.Task('main').done('switched · verified')
        finally:
            ui.DRY_RUN = saved
        line = err.getvalue()
        self.assertTrue(line.startswith('–'), line)
        self.assertIn('(dry run)', line)
        self.assertNotIn('verified', line)
        self.assertNotIn('✓', line)


class RobotTest(CliTestCase):
    def robots_text(self):
        with open(self.ws.robots_path(), encoding='utf-8') as f:
            return f.read()

    def test_ships_only_sim(self):
        rc, out, _ = self.run_cli('robot', 'list', '--json')
        self.assertEqual(rc, 0)
        self.assertEqual([r['name'] for r in json.loads(out)], ['sim'])

    def test_add_then_remove_keeps_comments(self):
        before = self.robots_text()
        rc, _, err = self.run_cli('robot', 'add', 'white', '--host', 'DIT-2026-11')
        self.assertEqual(rc, 0, err)
        r = Workspace(self.ws.root).robots()['white']
        self.assertEqual((r.host, r.domain_id), ('main@dit-2026-11.local', '11'))
        self.assertEqual((r.arch, r.tag), ('amd64', 'stable'))
        self.assertEqual(r.profiles, ['main', 'localization'])
        self.assertTrue(self.robots_text().startswith(before))
        rc, _, _ = self.run_cli('robot', 'remove', 'white')
        self.assertEqual(rc, 2)  # no terminal: needs --yes
        rc, _, err = self.run_cli('robot', 'remove', 'white', '--yes')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.robots_text(), before)

    def test_dit_hosts_use_your_own_team_account(self):
        rc, _, err = self.run_cli('robot', 'add', 'white', '--host', 'DIT-2026-11')
        self.assertEqual(rc, 0, err)
        self.assertIn('host = dit-2026-11.local\n', self.robots_text())  # shared file: no login
        with mock.patch.dict(os.environ, {'DIT_ROBOT_USER': 'vision'}):
            host = Workspace(self.ws.root).robots()['white'].host
        self.assertEqual(host, 'vision@dit-2026-11.local')

    def test_stack_env_points_at_an_existing_cyclonedds_config(self):
        # a subnet robot once got file:///opt/eurobot/cyclonedds-xml and restart-looped
        self.run_cli('robot', 'add', 'white', '--host', 'DIT-2026-11')
        ws = Workspace(self.ws.root)
        args = cli.argparse.Namespace(dry_run=True)
        uris = {
            n: cli.Target(ws, r, args).stack_env()['CYCLONEDDS_URI'] for n, r in ws.robots().items()
        }
        self.assertEqual(uris['white'], 'file:///opt/eurobot/cyclonedds.xml')
        self.assertEqual(uris['sim'], 'file:///opt/eurobot/cyclonedds-localhost.xml')
        for uri in uris.values():  # the image copies docker/cyclonedds*.xml to /opt/eurobot/
            name = uri.rsplit('/', 1)[1]
            self.assertTrue(os.path.isfile(os.path.join(ROOT, 'docker', name)), name)

    def test_contract_live_retries_without_domains_on_an_older_image(self):
        old = cli.subprocess.CompletedProcess(
            [], 2, 'contract_check: error: unrecognized arguments: --domains=main', ''
        )
        ok = cli.subprocess.CompletedProcess([], 0, 'OK    8 topics against contract.yaml', '')
        with (
            mock.patch.object(cli.Target, 'running_service', return_value='main'),
            mock.patch.object(cli.Target, 'sh', side_effect=[old, ok]) as sh,
        ):
            rc, _, err = self.run_cli('contract', 'live', '-r', 'sim')
        self.assertEqual(rc, 0, err)
        self.assertIn('predates --domains', err)
        first, second = (c.args[0] for c in sh.call_args_list)
        self.assertIn('--domains=', first)
        self.assertNotIn('--domains', second)

    def test_foreign_topics_reach_contract_live(self):
        with open(self.ws.robots_path(), 'a', encoding='utf-8') as f:
            f.write(
                '\n[white]\nhost = local\nprofiles = main\n'
                'foreign_topics = /robot/startup/game_time, /robot/startup/ideal_*\n'
            )
        robot = Workspace(self.ws.root).robots()['white']
        self.assertEqual(
            robot.foreign_topics, ['/robot/startup/game_time', '/robot/startup/ideal_*']
        )
        ok = cli.subprocess.CompletedProcess([], 0, b'OK    8 topics against contract.yaml\n', b'')
        with (
            mock.patch.object(cli.Target, 'running_service', return_value='main'),
            mock.patch.object(cli.subprocess, 'run', return_value=ok) as run,
        ):
            rc, _, err = self.run_cli('contract', 'live', '-r', 'white')
        self.assertEqual(rc, 0, err)
        cmd = ' '.join(map(str, run.call_args.args[0]))
        self.assertIn(
            "--ignore='/robot/startup/game_time,/robot/startup/ideal_*'", cmd
        )  # quoted: *

    def test_add_warns_when_the_domain_id_differs_from_the_hostname(self):
        rc, _, err = self.run_cli(
            'robot', 'add', 'white', '--host', 'DIT-2026-11', '--domain-id', '12'
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(Workspace(self.ws.root).robots()['white'].domain_id, '12')
        self.assertIn('differs from 11', err)

    def test_robot_env_domain_id_reads_etc_environment(self):
        done = mock.Mock(returncode=0, stdout='"11"\n')
        with mock.patch.object(cli.subprocess, 'run', return_value=done) as run:
            self.assertEqual(cli.robot_env_domain_id('main@dit-2026-11.local'), '11')
        self.assertIn('/etc/environment', run.call_args.args[0][-1])
        with mock.patch.object(cli.subprocess, 'run', return_value=mock.Mock(returncode=255)):
            self.assertIsNone(cli.robot_env_domain_id('main@dit-2026-11.local'))

    def test_add_picks_a_free_domain_id_and_refuses_duplicates(self):
        self.run_cli('robot', 'add', 'a', '--host', 'dit@a')
        self.run_cli('robot', 'add', 'b', '--host', 'dit@b')
        robots = Workspace(self.ws.root).robots()
        self.assertEqual((robots['a'].domain_id, robots['b'].domain_id), ('10', '11'))
        rc, _, err = self.run_cli('robot', 'add', 'a', '--host', 'dit@a')
        self.assertEqual(rc, 1)
        self.assertIn('already exists', err)

    def test_resolve_host_follows_dit_scripts_names(self):
        cases = {
            ('DIT-2026-11', None): ('dit-2026-11.local', '11'),  # login added per person
            ('dit-2026-13.local', 'navigation'): ('navigation@dit-2026-13.local', '13'),
            ('vision@DIT-2025-10', 'main'): ('vision@dit-2025-10.local', '10'),
            ('dit@192.168.0.50', None): ('dit@192.168.0.50', None),
            ('lab-nuc', 'main'): ('main@lab-nuc', None),
            ('lab-nuc', None): ('lab-nuc', None),  # an ~/.ssh/config alias carries its own user
        }
        for (host, user), want in cases.items():
            self.assertEqual(cli.resolve_host(host, user), want, host)

    def test_explicit_domain_id_wins_over_hostname(self):
        rc, _, err = self.run_cli('robot', 'add', 'w', '--host', 'DIT-2026-11', '--domain-id', '20')
        self.assertEqual(rc, 0, err)
        self.assertEqual(Workspace(self.ws.root).robots()['w'].domain_id, '20')

    def test_dry_run_changes_nothing(self):
        before = self.robots_text()
        rc, _, err = self.run_cli('-n', 'robot', 'add', 'white', '--host', 'dit@w')
        self.assertEqual(rc, 0)
        self.assertIn('(dry run)', err)
        self.assertEqual(self.robots_text(), before)

    def test_remove_unknown(self):
        rc, _, err = self.run_cli('robot', 'remove', 'nope', '--yes')
        self.assertEqual(rc, 1)
        self.assertIn('no robot nope', err)


class OutputTest(CliTestCase):
    def test_choices_explained_in_help(self):
        rc, out, _ = self.run_cli('help', 'build')
        self.assertEqual(rc, 0)
        self.assertIn('Choices:', out)
        self.assertRegex(out, r'RelWithDebInfo +optimized \+ debug symbols')

    def test_planned_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ui.planned('main', 'would colcon build domains/main')
        self.assertRegex(err.getvalue(), r'^– main +would colcon build domains/main  \(dry run\)')

    def test_build_dry_run_is_a_plan_not_a_script(self):
        with mock.patch.object(cli, 'ensure_image') as ensure:
            rc, _, err = self.run_cli('-n', 'build', '-d', 'main')
        self.assertEqual(rc, 0, err)
        ensure.assert_not_called()
        self.assertIn('would colcon build domains/main', err)
        self.assertNotIn('docker run', err)
        self.assertNotIn('colcon build --base-paths', err)


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.ws = make_ws()

    def tearDown(self):
        shutil.rmtree(self.ws.root)

    def test_global_flags_before_the_command(self):
        self.assertEqual(complete(self.ws, ['-n', 'b']), ['build'])
        self.assertIn('--version', complete(self.ws, ['-']))

    def test_flag_equals_value(self):
        self.assertEqual(
            complete(self.ws, ['build', '--build-type=Rel']),
            ['--build-type=RelWithDebInfo', '--build-type=Release'],
        )

    def test_short_flags_and_auto(self):
        self.assertIn('-d', complete(self.ws, ['deploy', '-']))
        self.assertEqual(complete(self.ws, ['build', '-j', '']), ['auto'])

    def test_no_filename_fallback_material(self):
        self.assertTrue(all(c.startswith('--') for c in complete(self.ws, ['status', ''])))

    def test_packages_follow_the_domain_filter(self):
        self.assertEqual(
            complete(self.ws, ['build', '-d', 'main', '-p', '']), ['main_bringup', 'main_core']
        )
        self.assertIn('localization_core', complete(self.ws, ['build', '-p', '']))

    def test_choice_values_have_their_own_meaning(self):
        got = complete(self.ws, ['build', '--build-type', 'D'], describe='--describe')
        self.assertEqual(got, ['Debug:no optimization: best for stepping through with gdb'])

    def test_describe_modes(self):
        self.assertEqual(
            complete(self.ws, ['deploy', '--p'], describe='--describe'),
            ['--persist:keep the overlay active after a reboot'],
        )
        tab = complete(self.ws, ['dep'], describe='--describe-tab')
        self.assertEqual(tab[0].split('\t')[0], 'deploy')


class CloneUrlTest(unittest.TestCase):
    URL = 'https://github.com/SeanChangX/Eurobot-Main.git'
    SSH = 'git@github.com:SeanChangX/Eurobot-Main.git'

    def ws_with_origin(self, origin):
        ws = mock.Mock()
        ws.git.return_value = origin
        return ws

    def test_ssh_shell_clones_domains_over_ssh(self):
        for origin in (
            'git@github.com:SeanChangX/DIT-X.git',
            'ssh://git@github.com/SeanChangX/DIT-X',
        ):
            ws = self.ws_with_origin(origin)
            self.assertEqual(cli.clone_url(ws, self.URL), self.SSH)
            self.assertEqual(cli.clone_url(ws, self.URL.removesuffix('.git')), self.SSH)

    def test_https_shell_and_ci_keep_the_manifest_url(self):
        for origin in ('https://github.com/SeanChangX/DIT-X', ''):
            self.assertEqual(cli.clone_url(self.ws_with_origin(origin), self.URL), self.URL)


if __name__ == '__main__':
    unittest.main()

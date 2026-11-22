```
    ____  __________   ____        __          __  _
   / __ \/  _/_  __/  / __ \____  / /_  ____  / /_(_)_________
  / / / // /  / /    / /_/ / __ \/ __ \/ __ \/ __/ / ___/ ___/
 / /_/ // /  / /    / _, _/ /_/ / /_/ / /_/ / /_/ / /__(__  )
/_____/___/ /_/    /_/ |_|\____/_.___/\____/\__/_/\___/____/
           DIT-X · Eurobot workspace · build, test, deploy
```

[![CI](https://github.com/SeanChangX/DIT-X/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/SeanChangX/DIT-X/actions/workflows/ci.yml)
[![Docs](https://github.com/SeanChangX/DIT-X/actions/workflows/docs.yml/badge.svg?branch=develop)](https://seanchangx.github.io/DIT-X/)
[![ROS 2 Humble | Jazzy](https://img.shields.io/badge/ROS%202-Humble%20%7C%20Jazzy-22314E?logo=ros)](https://docs.ros.org/en/humble/)
[![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white)](docker/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![ditx TUI: pick a command, toggle its options, see the exact command line](docs/assets/term/tui-build-dry-run.svg)

This is the **shell repo** of the Eurobot software. It holds the tooling, CI/CD, images and the
shared interfaces that every team builds on. The team code lives in domain repos
(`Eurobot-Main`, `Eurobot-Navigation`, `Eurobot-Localization`, `Eurobot-Vision`) that
`ditx sync` puts under `domains/`.

The repo is season-independent: each year continues on `develop`, and releases are
[SemVer](https://semver.org) tags on `main` (`v0.1.0`, `v1.0.0`), derived from the Conventional
Commits since the last release (`!` → major, `feat` → minor, `fix` → patch). The season goes in
the release title (e.g. "v1.2.0 — Eurobot 2027 final"). No more `Eurobot-<YEAR>-*` copies.

**Manual (中文教學)**: <https://seanchangx.github.io/DIT-X/>, built from `docs/` by
`.github/workflows/docs.yml` (enable Pages → Source: GitHub Actions once). Local preview: `ditx docs`.

## Quick start

Requirements: Linux with **git**, **python3**, **Docker** (with compose and buildx). Nothing else
goes on the host. ROS, compilers and linters all live in images.

```bash
git clone https://github.com/SeanChangX/DIT-X.git && cd DIT-X
./install.sh               # first: puts `ditx` on PATH + Tab completion (undo: --uninstall)
ditx init                  # your team account on the robots, default robot, ROS distro
ditx doctor                # checks docker, disk, RAM budget, install, robots
ditx sync                  # clone the domain repos into domains/
ditx build && ditx test --integration   # pulls the dev image CI publishes (~1 GB, once)
ditx                       # no arguments: interactive command builder (TUI)
```

Only the `sim` robot (this machine) ships in `config/robots.ini`. Add real robots once, like
git remotes: `ditx robot add white --host DIT-2026-11`.

The TUI is a command builder. You pick a command and options, the equivalent `ditx …` line is
shown at the bottom, and `r` runs exactly that line. Everything it can do, the CLI can do, and the
other way round.

## Commands

| | |
|---|---|
| `ditx build [-d DOMAIN] [-p PKG] [--base] [--arch arm64] [-j N] [--workers N] [--clangd]` | incremental build in the dev container, one install layer per domain; `-j`/`--workers` default to a RAM-safe budget |
| `ditx test [-d DOMAIN] [--integration]` | colcon unit tests; `--integration` starts all domains and checks handshake + contract |
| `ditx lint [--fix]` | clang-format, ruff, shellcheck, contract, domain boundaries (exactly what CI runs) |
| `ditx index` | `compile_commands.json` for clangd |
| `ditx new DOMAIN PACKAGE [--lang cpp\|python]` | new package from a template that already works |
| `ditx deploy -d DOMAIN -r ROBOT` | build → push overlay → restart → verify → auto rollback |
| `ditx overlays / rollback / logs / up / down -r ROBOT` | what runs where, undo, logs, runtime stack |
| `ditx robot [list\|add\|remove] [NAME] [--host H] [--domain-id N]` | manage `config/robots.ini` like `git remote` (comments are kept) |
| `ditx contract [validate\|md\|live]` | interface contract tooling |
| `ditx plugins` · `ditx <plugin>` | team tools: any executable `ditx-<name>` in `tools/ext/` (or on PATH) runs as `ditx <name>`, git/kubectl style; e.g. `ditx topics -r sim`, `ditx app rviz -r sim` (RViz / Foxglove / Gazebo via [ROS2X](https://github.com/SeanChangX/ROS2X)'s Humble image) |
| `ditx list` · `ditx cd <domain\|package>` | what is in each domain; jump to a package directory (like colcon_cd) |
| `ditx sync / status / image / clean / docs / ci STAGE` | `sync` fast-forwards this repo, then the domains, and installs the git hooks |
| `ditx shell [--cmd CMD]` | a shell (or one command) in the dev container with everything sourced |
| `ditx init [--defaults]` | write your local `.env` (your team = robot login, default robot, ROS distro) |
| `ditx` · `ditx tui` · `ditx -n` | the interactive command builder (`-n`: start in dry-run) |
| `ditx help [COMMAND]` | overview, or one command with examples (same as `--help`) |
| `ditx completion bash\|zsh\|fish` | print the completion script (`./install.sh` sets it up for you) |

Global flags work before or after the command: `-n/--dry-run` prints what would run and changes
nothing, `-v/--verbose` shows every tool's output, `-q/--quiet` prints only warnings and errors.
`status`, `list`, `overlays`, `plugins`, `robot` and `doctor` take `--json` for scripts. Destructive
commands (`rollback`, `down`, `clean`) ask first; `-y/--yes` skips the question.

## Layout

```
DIT-X/
├── bin/ditx                 entry point (Python standard library only)
├── tools/                   ditx sources, TUI, templates, tests, git hooks
├── base/                    shared ROS packages, baked into every image
│   ├── eurobot_interfaces/  msgs / srvs used between domains
│   └── eurobot_common/      contract.yaml (topics + QoS) + helpers
├── docker/                  Dockerfile, bake file, robot compose, cross/
├── config/robots.ini        robots: host, arch, domain id, what runs, tag
├── ditx.repos               domain repos (vcstool format)
├── infra/robot/             robot setup: folders, log timers, udev rules
├── docs/                    the manual (MkDocs), design notes, ADRs
├── .github/ .gitlab-ci.yml  CI/CD (both call `ditx ci <stage>`)
└── domains/                 (git-ignored) domain repos, cloned by `ditx sync`
```

## Read next

1. [docs/onboarding.md](docs/onboarding.md): first day, step by step (中文)
2. [docs/architecture.md](docs/architecture.md): shell, domains, layers, overlays and why
3. [docs/workflow.md](docs/workflow.md): develop → deploy → merge → release
4. [docs/logging.md](docs/logging.md) · [docs/middleware.md](docs/middleware.md): logs, clocks, QoS, DDS
5. [CONTRIBUTING.md](CONTRIBUTING.md): branches, commits, reviews, rules for AI-written code

## Moving the repos

The five repos live in a personal account for now (`SeanChangX`). To move them to an
organization or to GitLab, transfer them and replace the owner in one go:

```bash
git grep -lE 'SeanChangX/(DIT-X|Eurobot-)|ghcr\.io/seanchangx|seanchangx\.github\.io' |
  xargs sed -i -E -e 's#SeanChangX/(DIT-X|Eurobot-)#<org>/\1#g' \
    -e 's#ghcr\.io/seanchangx#ghcr.io/<org>#g' -e 's#seanchangx\.github\.io#<org>.github.io#g'
```

Run it in every domain repo too, then set the teams in `CODEOWNERS`. With a
registry available, CI can publish images again (see the header of `.github/workflows/ci.yml`).

## Credits

Created by [Sean Chang](https://github.com/SeanChangX) ([scx.tw/links](https://scx.tw/links)) for DIT Robotics in 2026,
and maintained by the DIT Robotics leads ([CODEOWNERS](.github/CODEOWNERS)). Every change since is in the git
history: `git log` / `git blame` show who changed what and why. MIT License.

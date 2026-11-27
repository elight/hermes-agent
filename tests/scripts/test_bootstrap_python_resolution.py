"""hermes_bootstrap_python resolves an interpreter across every store rung.

A dispatcher-spawned worker shell exports HERMES_HOME as a profile directory
that usually has no tool store of its own, while the account has already
installed its interpreters under $HOME/.hermes/tools. Resolution must fall
through to the account store when the profile store misses (the last rung),
while a profile that does have a store keeps winning over it. When every rung
misses the failure names every path it searched, in order — a bare "run
setup-hermes.sh" sends workers hunting the filesystem for the missing install.

The resolvers are driven by sourcing the real scripts/_activation.sh in bash
with faked HOME/HERMES_HOME (the child never sees the developer's home); a
store entry is just an executable stub, because resolution only asks for -x.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVATION = REPO_ROOT / "scripts" / "_activation.sh"


def _bash() -> str:
    found = shutil.which("bash")
    assert found, "bash is required to drive scripts/_activation.sh"
    return found


def _interpreter(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _store_entry(store: Path, version: str) -> Path:
    """A store laid out like pm's: python-<version>/bin/python3."""
    return _interpreter(store / f"python-{version}" / "bin" / "python3")


def _store_globs(store: Path) -> list[str]:
    """The candidate patterns the resolver reports searching inside one store."""
    return [
        f"{store}/python-*/bin/python3",
        f"{store}/python-*/python.exe",
        f"{store}/python-*/bin/python",
        f"{store}/python-*/bin/python.exe",
    ]


def _shape(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A checkout, an account home, a profile inside it, and a runtime store."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".hermes" / "profiles" / "worker"
    profile.mkdir(parents=True)
    runtime_store = tmp_path / "runtime-store"
    runtime_store.mkdir()
    return repo, home, profile, runtime_store


def _run(tmp_path_or_repo, script: str, *, home: Path, hermes_home=None, runtime_dir=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    if runtime_dir is not None:
        env["HERMES_RUNTIME_DIR"] = str(runtime_dir)
    return subprocess.run(
        [_bash(), "-c", script],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )


def _resolve(repo: Path, *, home: Path, hermes_home=None, runtime_dir=None):
    script = (
        f'. {shlex.quote(str(ACTIVATION))} && '
        f'hermes_bootstrap_python {shlex.quote(str(repo))}'
    )
    return _run(repo, script, home=home, hermes_home=hermes_home, runtime_dir=runtime_dir)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("missing", [0, 1, 2, 3, 4])
def test_the_highest_executable_rung_wins_whatever_sits_above_it_is_removed(tmp_path, missing):
    """The full order: checkout venv, HERMES_RUNTIME_DIR, $repo/../tools, the
    HERMES_HOME store, then the account store under $HOME/.hermes/tools."""
    repo, home, profile, runtime_store = _shape(tmp_path)
    created = [
        _interpreter(repo / ".venv" / "bin" / "python"),
        _store_entry(runtime_store, "11.1.1"),
        _store_entry(repo.parent / "tools", "22.2.2"),        # $repo/../tools
        _store_entry(profile / "tools", "33.3.3"),            # $HERMES_HOME/tools
        _store_entry(home / ".hermes" / "tools", "44.4.4"),   # $HOME/.hermes/tools
    ]
    # The resolver prints the match as it searched it; the ../tools rung is
    # written with its `..` intact and never normalized.
    expected = [str(entry) for entry in created]
    expected[2] = f"{repo}/../tools/python-22.2.2/bin/python3"
    for removed in created[:missing]:
        removed.unlink()
    result = _resolve(repo, home=home, hermes_home=profile, runtime_dir=runtime_store)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected[missing]


@pytest.mark.platforms("posix")
def test_a_profile_shell_without_a_profile_store_falls_through_to_the_account_store(tmp_path):
    """The worker-shell bug: a fresh worktree with no venv, no ../tools and no
    HERMES_RUNTIME_DIR; the account installed the interpreter, only the
    profile lacks a store, and the hook must still resolve it."""
    repo, home, profile, _runtime = _shape(tmp_path)
    account = _store_entry(home / ".hermes" / "tools", "44.4.4")
    result = _resolve(repo, home=home, hermes_home=profile)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(account)


@pytest.mark.platforms("posix")
def test_a_profile_that_has_a_store_keeps_beating_the_account_store(tmp_path):
    repo, home, profile, _runtime = _shape(tmp_path)
    mine = _store_entry(profile / "tools", "33.3.3")
    _store_entry(home / ".hermes" / "tools", "44.4.4")
    result = _resolve(repo, home=home, hermes_home=profile)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(mine)


@pytest.mark.platforms("posix")
def test_every_rung_missing_reports_every_searched_path_in_order(tmp_path):
    repo, home, profile, runtime_store = _shape(tmp_path)
    result = _resolve(repo, home=home, hermes_home=profile, runtime_dir=runtime_store)
    assert result.returncode == 1
    lines = result.stderr.splitlines()
    assert lines[0] == "no bootstrap Python found; run setup-hermes.sh"
    assert lines[1] == "searched, in order:"
    expected = [
        f"{repo}/.venv/bin/python",
        f"{repo}/.venv/Scripts/python.exe",
        f"{repo}/venv/bin/python",
        f"{repo}/venv/Scripts/python.exe",
        *_store_globs(runtime_store),
        *_store_globs(Path(f"{repo}/../tools")),
        *_store_globs(profile / "tools"),
        *_store_globs(home / ".hermes" / "tools"),
    ]
    assert lines[2:] == [f"  {path}" for path in expected]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("home_form", ["unset", "default", "default-slash"])
def test_the_account_store_is_searched_once_when_hermes_home_is_the_default_home(
    tmp_path, home_form
):
    """HERMES_HOME unset, equal to $HOME/.hermes, or that with a trailing
    slash: the default store and the fallback are one path, and a path is
    probed once."""
    repo, home, _profile, _runtime = _shape(tmp_path)
    hermes_home = None
    if home_form != "unset":
        hermes_home = str(home / ".hermes") + ("/" if home_form == "default-slash" else "")
    result = _resolve(repo, home=home, hermes_home=hermes_home)
    assert result.returncode == 1
    listing = result.stderr.splitlines()[2:]
    for path in _store_globs(home / ".hermes" / "tools"):
        assert listing.count(f"  {path}") == 1, result.stderr


@pytest.mark.platforms("posix")
def test_compose_env_reports_the_miss_once_with_the_searched_paths(tmp_path):
    """hermes_compose_env is what the git-hook chain runs; its failure is the
    resolvers report, printed once."""
    repo, home, profile, _runtime = _shape(tmp_path)
    script = (
        f'. {shlex.quote(str(ACTIVATION))} && '
        f'hermes_compose_env {shlex.quote(str(repo))} sh'
    )
    result = _run(repo, script, home=home, hermes_home=profile)
    assert result.returncode == 1
    assert result.stderr.count("no bootstrap Python found; run setup-hermes.sh") == 1
    assert "  " + str(home / ".hermes" / "tools" / "python-*" / "bin" / "python3") in result.stderr

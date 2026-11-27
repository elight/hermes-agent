"""``activate`` / ``activate.ps1``: an interactive shell takes on the environment and gives it back.

What a process launched under ``scripts/run-in-hermes-env`` sees is covered in
tests/scripts/test_run_in_hermes_env.py; how the environment is composed, in
test_environment_script.py; the fish port, in test_activate_fish.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.pm.activation_support import (
    ACTIVATE, ACTIVATE_PS1, CANARY, SETUP_HERMES_PS1, SETUP_HERMES_SH, bash, bash_env,
    child_env, fake_store, isolated_checkout, posix, powershell, spawnable_python,
    venv_entry,
)


def test_bash_scripts_pass_syntax_check():
    for script in (ACTIVATE, SETUP_HERMES_SH):
        result = subprocess.run(
            [bash(), "-n", posix(script)], capture_output=True, text=True, env=child_env(),
            check=False,
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


@pytest.mark.platforms("windows")
def test_source_activate_exports_the_pm_env(tmp_path: Path):
    root = isolated_checkout(tmp_path)
    store, _ = fake_store(tmp_path)
    script = (
        f'source "{posix(root / "activate")}" && '
        f'test -n "$__HERMES_ACTIVATED" && '
        f'printf "%s" "${CANARY}"'
    )
    result = subprocess.run(
        [bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=posix(tmp_path),
        env=bash_env(store),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "env-ok"


@pytest.mark.platforms("windows")
def test_activate_exports_the_sentinel_to_child_processes(tmp_path: Path):
    """Repo scripts read activation from the environment, so the sentinel must
    survive into an exec'd child (a plain shell variable would not), and
    deactivate must take it back out."""
    root = isolated_checkout(tmp_path)
    store, _ = fake_store(tmp_path)
    script = (
        f'source "{posix(root / "activate")}" && '
        f'"$BASH" -c \'test -n "$__HERMES_ACTIVATED"\' && '
        f'deactivate && '
        f'! "$BASH" -c \'test -n "$__HERMES_ACTIVATED"\' && '
        f'echo exported-then-cleared'
    )
    result = subprocess.run(
        [bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=posix(tmp_path),
        env=bash_env(store),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "exported-then-cleared"


@pytest.mark.platforms("windows")
def test_deactivate_restores_the_prior_shell(tmp_path: Path):
    root = isolated_checkout(tmp_path)
    store, _ = fake_store(tmp_path)
    script = (
        f'source "{posix(root / "activate")}" && deactivate && '
        f'test -z "${{{CANARY}+set}}" && '
        f'test -z "${{__HERMES_ACTIVATED+set}}" && '
        f"! declare -F deactivate >/dev/null && "
        f'echo restored'
    )
    result = subprocess.run(
        [bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=posix(tmp_path),
        env=bash_env(store),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "restored"


@pytest.mark.platforms("windows")
def test_activate_leaves_the_shell_paths_in_posix_form(tmp_path: Path):
    """The pm env is read by a native Windows Python, which sees PATH, HOME and
    the temp variables in Windows form. Exported verbatim, `C:\\a;C:\\b` left
    bash without a usable PATH, so every command after activation failed."""
    root = isolated_checkout(tmp_path)
    store, _ = fake_store(tmp_path)
    script = (
        'prior_home="$HOME" prior_tmp="$TMP" && '
        f'source "{posix(root / "activate")}" && '
        'command -v basename >/dev/null && '
        'case "$PATH" in *";"*|*"\\\\"*) exit 3;; esac && '
        'test "$HOME" = "$prior_home" && test "$TMP" = "$prior_tmp" && '
        'echo posix'
    )
    result = subprocess.run(
        [bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=posix(tmp_path),
        env=bash_env(store),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "posix"


def test_activate_fails_cleanly_without_a_store(tmp_path: Path):
    env = bash_env(tmp_path / "empty-store")
    isolated = isolated_checkout(tmp_path) / "activate"
    script = (
        f'source "{posix(isolated)}" 2>/dev/null; '
        f'test $? -ne 0 && echo refused'
    )
    result = subprocess.run(
        [bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=posix(tmp_path),
        env=env,
        check=False,
    )
    # Without any provisioned python the source must refuse — never
    # silently no-op with a half-activated shell.
    assert "refused" in result.stdout


def test_powershell_scripts_parse():
    """Parse-check the PowerShell entry points; skip gracefully when no
    PowerShell host is available."""
    ps = powershell()
    if ps is None:
        pytest.skip("no PowerShell host available")
    for script in (ACTIVATE_PS1, SETUP_HERMES_PS1):
        result = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"$errs = $null; $null = [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{script}', [ref]$null, [ref]$errs); "
                f"if ($errs.Count) {{ $errs | ForEach-Object {{ $_.Message }}; exit 1 }}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{script.name}: {result.stdout}{result.stderr}"


@pytest.mark.platforms("windows")
def test_powershell_activate_exports_and_deactivates(tmp_path: Path):
    # Native venv redirectors resolve the base DLLs/stdlib without copying CPython.
    root = isolated_checkout(tmp_path)
    env = bash_env(tmp_path / "store")
    subprocess.run(
        [str(spawnable_python()), "-m", "venv", "--without-pip", str(root / ".venv")],
        check=True, capture_output=True, env=env, timeout=60,
    )
    ps = powershell()
    assert ps, "native Windows test requires PowerShell"
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         f"$ErrorActionPreference='Stop'; $env:PYTHONPATH='caller-original'; "
         f". '{root / 'activate.ps1'}'; "
         "Write-Output ('active=' + $env:PYTHONPATH); deactivate; "
         "Write-Output ('after=' + $env:PYTHONPATH)"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Nothing is committed, so the checkout alone: the bootstrap .venv's packages never leak in.
    assert f"active={root}\n" in result.stdout
    assert "after=caller-original" in result.stdout


# --- bootstrap Python account-store fallback (Windows) ---------------------
#
# A profile shell (HERMES_HOME=profiles/<name>) with no tools of its own must
# resolve the bootstrap interpreter from the account store, after the profile's
# own tools rung misses — the same search order as scripts/_activation.sh.
# Regression for the profile-shell breakage where activation refused with
# "no bootstrap Python found" although the account store held the interpreter.

def _profile_shell_env(tmp_path: Path, *, hermes_home: Path | None,
                       localappdata: Path) -> dict:
    """Env for a shell that starts unprovisioned everywhere: HOME-ish at
    tmp_path, HERMES_HOME optionally set (unset models the default install),
    LOCALAPPDATA the account root the fallback resolves. No runtime dir, no
    Python vars."""
    env = child_env()
    home = tmp_path / "profile-home"
    home.mkdir(exist_ok=True)
    env.update(HOME=posix(home), USERPROFILE=str(home))
    if hermes_home is not None:
        hermes_home.mkdir(parents=True, exist_ok=True)
        env["HERMES_HOME"] = posix(hermes_home)
    else:
        env.pop("HERMES_HOME", None)
    env["LOCALAPPDATA"] = str(localappdata)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "BASH_ENV",
                "__HERMES_ACTIVATED", "HERMES_RUNTIME_DIR", CANARY):
        env.pop(key, None)
    return env


def _source_activate(ps: str, script: Path, env: dict, cwd: Path):
    return subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         f"$ErrorActionPreference='Stop'; . '{script}'; "
         "Write-Output ('sentinel-file=' + [bool](Test-Path $env:__HERMES_ACTIVATED))"],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=60, check=False,
    )


@pytest.mark.platforms("windows")
def test_activate_falls_back_to_the_account_store_from_a_profile_shell(tmp_path: Path):
    """The bug: a profile's HERMES_HOME has no tools dir; the interpreter is
    the account store's. On the base commit activation throws
    "no bootstrap Python found" here — red on base, green with the fallback."""
    root = isolated_checkout(tmp_path)
    account = tmp_path / "AppData"
    venv_entry(account / "hermes" / "tools", "python-3.11.9")
    env = _profile_shell_env(tmp_path, hermes_home=tmp_path / "profile-home" / "hermes",
                             localappdata=account)
    ps = powershell()
    assert ps, "native Windows test requires PowerShell"
    result = _source_activate(ps, root / "activate.ps1", env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sentinel-file=True" in result.stdout, result.stdout + result.stderr


def _activate_refusal(tmp_path: Path, env: dict) -> str:
    """Source activate.ps1 in an unprovisioned world; return its failure text.
    Whitespace-flattened: PowerShell word-wraps the ErrorRecord at the console
    width, and these paths exceed it — a raw substring hit would be a
    line-length coincidence, not an assertion about the message."""
    root = isolated_checkout(tmp_path)
    ps = powershell()
    assert ps, "native Windows test requires PowerShell"
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         f"$ErrorActionPreference='Stop'; . '{root / 'activate.ps1'}'"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=60, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    return " ".join((result.stdout + result.stderr).split())


@pytest.mark.platforms("windows")
def test_activate_missing_everywhere_names_every_searched_root(tmp_path: Path):
    """Nothing provisioned anywhere: the refusal carries each root in search
    order (checkout-adjacent, profile tools, account tools) plus the
    remediation, so the user sees what to fix, not just that something is
    missing."""
    env = _profile_shell_env(tmp_path, hermes_home=tmp_path / "profile-home" / "hermes",
                             localappdata=tmp_path / "AppData")
    text = _activate_refusal(tmp_path, env)
    # Roots are listed as searched — the checkout-adjacent rung is the raw
    # "<checkout>\..\tools" string, not a normalized path.
    repo_adjacent = str(tmp_path / "checkout with spaces" / ".." / "tools")
    profile_tools = str(tmp_path / "profile-home" / "hermes" / "tools")
    account_tools = str(tmp_path / "AppData" / "hermes" / "tools")
    for path in (repo_adjacent, profile_tools, account_tools):
        assert path in text, f"{path} missing from the refusal:\n{text}"
    assert (text.index(repo_adjacent) < text.index(profile_tools)
            < text.index(account_tools)), f"roots out of search order:\n{text}"
    assert "run setup-hermes.ps1" in text, text


@pytest.mark.platforms("windows")
def test_default_home_searches_the_account_store_once(tmp_path: Path):
    """Default install (HERMES_HOME unset): the default-home rung and the
    account store are the same directory — the refusal must not name it
    twice, so the list keeps describing distinct places actually searched."""
    env = _profile_shell_env(tmp_path, hermes_home=None, localappdata=tmp_path / "AppData")
    text = _activate_refusal(tmp_path, env)
    account_tools = str(tmp_path / "AppData" / "hermes" / "tools")
    assert account_tools in text, text
    assert text.count(account_tools) == 1, f"account store listed twice:\n{text}"


@pytest.mark.platforms("windows")
def test_activate_prefers_a_profile_store_over_the_account_store(tmp_path: Path):
    """Ordering invariant: the fallback goes AFTER the profile's own tools.
    The account-store entry is a non-spawnable stub, so a resolver that
    consulted it first could not complete activation with a clean sentinel."""
    root = isolated_checkout(tmp_path)
    account = tmp_path / "AppData"
    stub = account / "hermes" / "tools" / "python-3.11.9"
    stub.mkdir(parents=True)
    (stub / "python.exe").write_text("not an interpreter", encoding="utf-8")
    venv_entry(tmp_path / "profile-home" / "hermes" / "tools", "python-3.11.9")
    env = _profile_shell_env(tmp_path, hermes_home=tmp_path / "profile-home" / "hermes",
                             localappdata=account)
    ps = powershell()
    assert ps, "native Windows test requires PowerShell"
    result = _source_activate(ps, root / "activate.ps1", env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sentinel-file=True" in result.stdout, result.stdout + result.stderr

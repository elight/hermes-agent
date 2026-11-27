"""The PS1 bootstrap fallback's decision logic, executed by a real PowerShell engine.

``activate.ps1`` is Windows-only, so its end-to-end contract lives in
``tests/pm/test_activate_scripts.py`` (platforms("windows")). This module
lifts the two decision blocks between their markers in the script and runs
them through any available PowerShell host — the search-order append and the
miss-report are string/array logic, so their engine behaviour holds on every
host even where the full script cannot run. No markers means the fallback is
not in the script: that is the red this test pins on the base commit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVATE_PS1 = REPO_ROOT / "activate.ps1"

ORDER_START = "# >>> bootstrap-account-store-fallback"
ORDER_END = "# <<< bootstrap-account-store-fallback"
MISS_START = "# >>> bootstrap-miss-report"
MISS_END = "# <<< bootstrap-miss-report"


def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _host() -> str:
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("no PowerShell host available")
    return pwsh


def _lift(start_marker: str, end_marker: str) -> str:
    """The block between the marker lines, verbatim from the script."""
    lines = ACTIVATE_PS1.read_text(encoding="utf-8").splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.strip().startswith(start_marker):
            start = i + 1
        elif start is not None and line.strip().startswith(end_marker):
            end = i
            break
    if start is None or end is None:
        pytest.fail(
            f"activate.ps1 has no {start_marker!r} block — the account-store "
            "fallback is not in the script (this test is red on the base commit)"
        )
    block = "\n".join(lines[start:end])
    assert block.strip(), f"empty {start_marker!r} block"
    return block


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    script = tmp_path / "lift.ps1"
    script.write_text(body, encoding="utf-8")
    return subprocess.run(
        [_host(), "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_account_store_appended_when_profile_tools_is_the_previous_rung(tmp_path: Path):
    """A profile shell's last rung is the profile's own tools dir; the account
    store must be appended after it, never before it."""
    body = (
        "$env:LOCALAPPDATA = 'C:\\Users\\evan\\AppData\\Local'\n"
        "$roots = @('C:\\runtime', 'C:\\repo\\..\\tools',"
        " 'C:\\Users\\evan\\.hermes\\profiles\\coding\\tools')\n"
        + _lift(ORDER_START, ORDER_END)
        + "\nWrite-Output $roots.Count\n$roots | ForEach-Object { Write-Output $_ }\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    out = result.stdout.splitlines()
    assert out[0] == "4", f"account store was not appended: {out}"
    assert out[-1] == "C:\\Users\\evan\\AppData\\Local\\hermes\\tools", out


def test_account_store_not_appended_when_it_is_already_the_previous_rung(tmp_path: Path):
    """Default install: the default-home rung and the account store are the
    same directory built by the same expression — searched once, not listed
    twice. The prelude builds the previous rung from that same expression,
    so the engine's equality — not the test's — decides."""
    body = (
        "$env:LOCALAPPDATA = 'C:\\Users\\evan\\AppData\\Local'\n"
        "$homeRootDefault = \"$env:LOCALAPPDATA\\hermes\"\n"
        "$roots = @('C:\\runtime', \"$homeRootDefault\\tools\")\n"
        + _lift(ORDER_START, ORDER_END)
        + "\nWrite-Output $roots.Count\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0] == "2", f"account store was duplicated: {result.stdout}"


def test_no_account_store_rung_when_localappdata_is_absent(tmp_path: Path):
    """No LOCALAPPDATA (interactive service hosts): no account-store rung —
    the search must not invent a path from an empty variable."""
    body = (
        "Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue\n"
        "$roots = @('C:\\runtime', 'C:\\tools')\n"
        + _lift(ORDER_START, ORDER_END)
        + "\nWrite-Output $roots.Count\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0] == "2", (
        f"a rung was invented from an empty LOCALAPPDATA: {result.stdout}"
    )


def test_total_miss_error_lists_every_searched_path_in_order(tmp_path: Path):
    """The throw names each root searched, in search order, skipping the
    unset HERMES_RUNTIME_DIR rung, and carries the remediation the user
    needs. Default-home de-duplication is not this block's job — the
    previous test pins the search never adding a rung twice."""
    body = (
        "$ErrorActionPreference = 'Stop'\n"
        "$roots = @($null, 'C:\\repo\\..\\tools',"
        " 'C:\\Users\\evan\\.hermes\\profiles\\coding\\tools',"
        " 'C:\\Users\\evan\\AppData\\Local\\hermes\\tools')\n"
        + "try {\n" + _lift(MISS_START, MISS_END)
        + "\n} catch { Write-Output $_.Exception.Message }\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    message = result.stdout.strip()
    assert message.startswith("activate:"), message
    for path in ("C:\\repo\\..\\tools",
                 "C:\\Users\\evan\\.hermes\\profiles\\coding\\tools",
                 "C:\\Users\\evan\\AppData\\Local\\hermes\\tools"):
        assert path in message, f"{path} missing from the error: {message}"
    assert (message.index("C:\\repo\\..\\tools") < message.index("profiles")
            < message.index("AppData")), f"paths out of search order: {message}"
    assert "run setup-hermes.ps1" in message, message
    assert "; ;" not in message and not message.endswith(";"), f"empty rung listed: {message}"

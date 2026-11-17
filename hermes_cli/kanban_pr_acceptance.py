"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")

# A PR URL mentioned anywhere in prose — a comment body, a run's metadata blob. Deliberately
# as broad as the respawn-guard detector it feeds (``http`` allowed, host any case): a
# narrower matcher would let a differently-spelled URL skip the guard altogether, which is
# a silent widening of a fail-closed rule. The number is normalised, so ``pull/007`` and
# ``pull/7`` key alike.
_PR_MENTION = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/([0-9]+)", re.IGNORECASE)


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _pr_keys(text: str | None) -> set[str]:
    """Canonical ``owner/repo/pull/N`` keys for every GitHub PR URL in ``text``.

    Its own detector rather than :data:`_PR`, because this one reads *prose*: it must
    stay as broad as the guard regex it replaces — ``http``, any host case — or a
    differently-spelled URL would quietly stop anchoring the guard at all. Keys are
    canonicalised (lower-case, scheme and host dropped) so the same PR compares
    equal on both sides of the attribution lookup.
    """
    return {f"{match[1]}/{match[2]}/pull/{int(match[3])}".lower()
            for match in _PR_MENTION.finditer(text or "")}


def _pr_publication_owners(conn, pr_key: str) -> set[str]:
    """Task ids whose own record publishes ``pr_key`` — the cards that opened it.

    Publication is written by the publishing card's own worker and never by a card
    that merely cites the PR, which makes it the one local distinction between
    "this card has a PR in flight" and "a comment pointed at someone else's". Two
    places record it: ``kanban_db`` lands a worker's metadata dict on its closing run
    (workers name the URL under free-form keys — measured in production boards:
    ``pr``, ``pr_head``, ``published_pr``), and :mod:`hermes_cli.kanban_pr_acceptance_store`
    binds an exact PR URL into ``tasks.completion_contract``. The URL *value* is
    therefore what identifies a publication, not a key name: a reader that looked
    only for ``published_pr`` saw nothing on a board whose workers wrote ``pr``.

    The SQL ``LIKE`` narrows candidates to rows carrying that ``owner/repo/pull/N``
    (SQLite's LIKE is case-insensitive for ASCII); Python then compares whole keys,
    so ``pull/140`` never vouches for ``pull/14``. An empty set means no card is
    recorded as publishing it — unknown authorship, not evidence of absence.
    """
    repo, _, number = pr_key.rpartition("/pull/")
    needle = f"%{repo}/pull/{number}%"
    owners: set[str] = set()
    for row in conn.execute("SELECT id AS task_id, completion_contract AS record FROM tasks"
                            " WHERE completion_contract LIKE ?", (needle,)):
        if pr_key in _pr_keys(row["record"]):
            owners.add(row["task_id"])
    for row in conn.execute("SELECT task_id, metadata AS record FROM task_runs WHERE metadata LIKE ?",
                            (needle,)):
        if pr_key in _pr_keys(row["record"]):
            owners.add(row["task_id"])
    return owners


def comment_anchors_active_pr(conn, task_id: str, body: str) -> bool:
    """Whether ``body`` (a comment on ``task_id``) evidences a PR *this* card has open.

    False only when every PR URL in the comment is recorded as some *other* card's
    publication: such a comment is coordination prose about a sibling's PR — the
    shape of a QA or hand-off note on a card whose job is to cite other cards — and
    holding its lane for 24 h strands a chain that has nothing waiting on a review.

    True otherwise, including the unknown case: a worker that reported its PR only
    in comment prose stays protected from a duplicate implementation (#111910). A
    comment citing several PRs anchors if any one of them is this card's or
    unattributed, so this rule can only narrow the guard, never widen it.
    """
    for pr_key in _pr_keys(body):
        owners = _pr_publication_owners(conn, pr_key)
        if not owners or task_id in owners:
            return True
    return False


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


# Every key gh can read a login from, github.com and GitHub Enterprise alike. All of them are
# dropped from the launcher's env for a routed child; only _GH_GITHUB_LOGIN_KEYS come back.
_GH_LOGIN_KEYS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
)
# Every call here targets github.com (`--hostname github.com`, github.com PR URLs), where gh
# ignores the enterprise token vars and GH_HOST. So only these are re-added from the profile, and
# only these prove a login: an enterprise-only profile must still get the fail-closed pin below,
# or gh falls through to the launcher's ~/.config/gh/hosts.yml for github.com.
_GH_GITHUB_LOGIN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")
# Not a login, but gh cannot reach github.com without them where the profile's .env sets them
# (a proxy in .env is documented; a routed child loses the launcher's .env copy).
_GH_TRANSPORT_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy",
                      "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). gh needs only its own login, so the env keeps
    the provider scrub — ``served_profile_child_env(inherit_credentials=False)``,
    its "caller re-adds the few keys the child needs" contract — and re-adds
    exactly gh's github.com login keys (``_GH_GITHUB_LOGIN_KEYS``) from the target profile's own secret scope
    instead of overlaying the whole scope (#134669: the gh child never needs
    the assignee's provider keys or adapter secrets).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from agent.secret_scope import build_profile_secret_scope
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=False)
    routed = _is_routed_home(profile_home)
    if routed:
        # The launcher's own gh login must not reach the child: GH_TOKEN/GITHUB_TOKEN are
        # already scrubbed by hermes_subprocess_env, but the enterprise trio (GH_HOST,
        # GH_/GITHUB_ENTERPRISE_TOKEN) is on no strip list, and a surviving ambient token
        # would reach the child (#122689, the GHE case). The target's own github.com login keys
        # are re-added from its secret scope below.
        for key in _GH_LOGIN_KEYS:
            base.pop(key, None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=False)
    scope = build_profile_secret_scope(Path(profile_home))
    env.update((key, scope[key]) for key in _GH_GITHUB_LOGIN_KEYS if scope.get(key))
    env.update((key, scope[key]) for key in _GH_TRANSPORT_KEYS if key in scope)  # NO_PROXY= clears
    if routed and not (env.keys() & set(_GH_GITHUB_LOGIN_KEYS)):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")

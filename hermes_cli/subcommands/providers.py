"""``hermes providers`` subcommand parser."""

from __future__ import annotations


def build_providers_parser(subparsers) -> None:
    """Attach the ``providers`` subcommand to ``subparsers``."""
    from hermes_cli.providers_cmd import cmd_providers
    from hermes_cli.subcommands._shared import add_json_flag

    providers_parser = subparsers.add_parser(
        "providers",
        help="Show locked provider paths and reopen them",
        description="A provider path that keeps failing hard (an exhausted quota, dead credits) is "
            "locked so nothing keeps retrying it, and every session on this host honours the lock "
            "before it dials. List the locked paths here and reopen one once the provider is "
            "healthy again.")
    providers_parser.add_argument(
        "--reopen", metavar="PATH", default=None,
        help="Clear the lock on one provider path and start dialing it again (scriptable)")
    add_json_flag(providers_parser, "Print the locked paths as JSON")
    providers_parser.add_argument(
        "--no-select", action="store_true",
        help="List only; do not offer to reopen a locked path interactively")
    providers_parser.set_defaults(func=cmd_providers)

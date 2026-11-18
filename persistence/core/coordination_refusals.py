"""Plain-English causes for ``runtime_coordination_required``.

One wire code covers several unrelated refusals. Each refusal names its cause in ``field`` and a
short sentence in ``detail``: what did not happen and, where the user can fix it, the next step.
Clients show ``detail``; logs and bug reports quote ``field``.
"""

from __future__ import annotations

from persistence.core.errors import AuthorityRefusal

REASON = 'runtime_coordination_required'

#: cause -> what the user reads. Short: a toast or one terminal line.
MESSAGES = {
    'open_elsewhere': "Couldn't open this chat: it's open in another window or app. Close it there, then try again.",
    'ownership_unknown': "Couldn't open this chat: Hermes couldn't check where else it's open. Restart Hermes, then try again.",
    'reply_in_progress': "Couldn't open this chat: a reply is still being written. Try again when it finishes.",
    'bot_chat_open_elsewhere': "Message not sent: the Bot Chat is open in another window. Close it there, then resend.",
    'no_local_settings': "Couldn't change this chat: it came from a messaging app, or hasn't been opened since an update. Open it, then retry.",
    'not_prepared': "Couldn't change this chat: a required step was skipped. Try again.",
    'editor_tools_not_ready': "Couldn't branch this chat: its editor tools aren't connected. Branch it from the editor.",
    'not_this_process': "Couldn't delete this chat: this Hermes process doesn't manage it. Restart Hermes, then try again.",
    'cannot_reset_running': "Couldn't do that: this Hermes backend can't reset a chat that is running.",
    'codex_manages_context': "Couldn't compress: the Codex runtime manages this chat's context itself.",
    'groups_not_running': "Couldn't do that: group chats aren't running. Start or restart Hermes, then try again.",
    'group_send_not_running': "Message not sent: group chats aren't running. Start or restart Hermes, then resend.",
    'group_still_working': "Couldn't disband: the group is still working. Stop it, then try again.",
    'group_access_held': "Couldn't disband: bots still have access. Start Hermes on the group's computer, then try again.",
}


def coordination_refusal(cause: str) -> AuthorityRefusal:
    """The ``runtime_coordination_required`` refusal for *cause*, carrying its field and message."""
    return AuthorityRefusal(REASON, field=cause, detail=MESSAGES[cause])

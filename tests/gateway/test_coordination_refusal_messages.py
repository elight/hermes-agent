"""``runtime_coordination_required`` covers unrelated causes; each one tells the user what failed (#157224).

The wire code stays the same (clients and the Bot DM tool compare it); ``field`` names the cause and
``detail`` is the short sentence a client shows instead of the code.
"""

import pytest

from persistence.core.coordination_refusals import MESSAGES, REASON, coordination_refusal


@pytest.mark.parametrize("cause", sorted(MESSAGES))
def test_each_cause_keeps_the_code_and_says_what_failed(cause):
    refusal = coordination_refusal(cause)
    assert (str(refusal), refusal.reason, refusal.field) == (REASON, REASON, cause)
    detail = refusal.detail
    assert detail == MESSAGES[cause], "the detail must survive the wire limit uncut"
    assert detail.startswith(("Couldn't", "Message not sent")), detail
    assert "_" not in detail, "no internal codes in what the user reads"


def test_the_bot_chat_held_refusal_still_settles_as_not_sent(tmp_path):
    """The Bot DM tool recognises a definite no-send by the code and ``admitted=False``."""
    import asyncio
    from gateway.session_bot import _held_by_another_writer
    from persistence.core.errors import AuthorityRefusal
    from tools.bot_failure_reasons import HELD_NOT_SENT

    held = asyncio.run(_held_by_another_writer(tmp_path, "a" * 64, AuthorityRefusal(HELD_NOT_SENT)))
    assert held is not None and held.reason == HELD_NOT_SENT and held.admitted is False
    assert held.field == "bot_chat_open_elsewhere" and "resend" in held.detail


def test_a_coordination_refusal_is_logged_at_info_not_warning(caplog):
    """A resume retry loop must not fill errors.log now that the refusal names its cause."""
    from gateway.session_controls import _log_refusal

    with caplog.at_level("INFO", logger="gateway.session_controls"):
        _log_refusal("test", "session.resume", coordination_refusal("open_elsewhere"))
    record = caplog.records[-1]
    assert record.levelname == "INFO" and "field=open_elsewhere" in record.getMessage()

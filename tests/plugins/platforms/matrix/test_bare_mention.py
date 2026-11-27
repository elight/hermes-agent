"""Bare ``@handle`` outbound mentions must become real mentions.

Literal ``@evan`` text never notifies anyone: clients and push rules key on MSC3952
``m.mentions.user_ids`` (or a matrix.to link in ``formatted_body``). The adapter resolves a
bare handle against the target room's joined members and emits both.
"""

import pytest


class _Members:
    def __init__(self, members):
        self.members = {m: None for m in members}


class _Client:
    def __init__(self, members, mxid="@bot:example.org", fail=False):
        self._members = members
        self.mxid = mxid
        self.calls = 0
        self._fail = fail

    async def joined_members(self, room_id):
        self.calls += 1
        if self._fail:
            raise RuntimeError("boom")
        return _Members(self._members)


def _adapter(members, mxid="@bot:example.org", fail=False):
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(
        enabled=True, token="syt_test_token",
        extra={"homeserver": "https://matrix.example.org", "user_id": mxid}))
    adapter._user_id = mxid
    adapter._client = _Client(members, mxid=mxid, fail=fail)
    return adapter


@pytest.mark.asyncio
async def test_bare_handle_resolves_to_mention_and_pill():
    a = _adapter(["@evan:example.org"])
    content = a._build_text_message_content("@evan — ready. Details follow.")
    assert "m.mentions" not in content  # pre-fix behaviour: literal text, no notification
    await a._resolve_bare_mentions("!room:example.org", content, "@evan — ready. Details follow.")
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    assert "https://matrix.to/#/@evan:example.org" in content["body"]
    assert "https://matrix.to/#/@evan:example.org" in content["formatted_body"]
    assert "<a " in content["formatted_body"]  # rendered as a pill link, not bare text


@pytest.mark.asyncio
async def test_full_mxid_is_not_double_handled():
    a = _adapter(["@evan:example.org"])
    text = "@evan:example.org ready"
    content = a._build_text_message_content(text)
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]  # no duplicate entry
    assert content["body"] == text  # plain body untouched
    assert content["formatted_body"].count("matrix.to") == 1


@pytest.mark.asyncio
async def test_unknown_and_ambiguous_handles_stay_literal():
    a = _adapter(["@evan:example.org", "@evan:other.org"])
    content = a._build_text_message_content("@evan ping")
    await a._resolve_bare_mentions("!room:example.org", content, "@evan ping")
    assert "m.mentions" not in content
    assert content["body"] == "@evan ping"

    b = _adapter(["@someone:example.org"])
    content = b._build_text_message_content("@ghost ping")
    await b._resolve_bare_mentions("!room:example.org", content, "@ghost ping")
    assert "m.mentions" not in content
    assert content["body"] == "@ghost ping"


@pytest.mark.asyncio
async def test_code_spans_and_links_are_not_touched():
    a = _adapter(["@evan:example.org"])
    text = "run `@evan` and see [profile](https://example.org/@evan) plus\n```\n@evan\n```"
    content = a._build_text_message_content(text)
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert "m.mentions" not in content
    assert content["body"] == text


@pytest.mark.asyncio
async def test_same_handle_inside_code_span_stays_literal_when_outside_resolves():
    a = _adapter(["@evan:example.org"])
    text = "run `@evan` then ping @evan now"
    content = a._build_text_message_content(text)
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    assert "`@evan`" in content["body"]  # the code-span occurrence is untouched
    assert content["body"].count("matrix.to") == 1  # only the outside occurrence became a pill


@pytest.mark.asyncio
async def test_transient_failure_is_not_negative_cached():
    a = _adapter(["@evan:example.org"], fail=True)
    content = a._build_text_message_content("@evan ping")
    await a._resolve_bare_mentions("!room:example.org", content, "@evan ping")
    assert "m.mentions" not in content  # first attempt failed -> literal
    a._client._fail = False  # endpoint recovers; no stale empty cache may block it
    content = a._build_text_message_content("@evan ping")
    await a._resolve_bare_mentions("!room:example.org", content, "@evan ping")
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]


@pytest.mark.asyncio
async def test_room_handle_never_resolves_to_a_member():
    a = _adapter(["@room:example.org"])
    text = "@room attention"
    content = a._build_text_message_content(text)
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert "m.mentions" not in content
    assert content["body"] == text


@pytest.mark.asyncio
async def test_plain_text_rewrite_gains_html():
    a = _adapter(["@evan:example.org"])
    text = "@evan — ready with a moderately long sentence so the html differs."
    content = a._build_text_message_content(text)
    assert "formatted_body" not in content  # builder skips html when text is markdown-free
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    assert content["format"] == "org.matrix.custom.html"  # pill markdown now needs html
    assert '<a href="https://matrix.to/#/@evan:example.org">' in content["formatted_body"]


@pytest.mark.asyncio
async def test_oversized_rewrite_drops_stale_html(monkeypatch):
    import plugins.platforms.matrix.adapter as mod
    a = _adapter(["@evan:example.org"])
    text = "**bold** @evan — ready with a moderately long sentence so the html differs."
    content = a._build_text_message_content(text)
    assert content.get("format") == "org.matrix.custom.html"  # bold made the builder emit html
    monkeypatch.setattr(mod, "_MAX_CONTENT_BYTES", 10)  # now force the over-budget branch
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    assert "matrix.to" in content["body"]
    assert "formatted_body" not in content and "format" not in content  # stale HTML dropped


@pytest.mark.asyncio
async def test_trailing_punctuation_and_colon_forms_resolve():
    a = _adapter(["@evan:example.org"])
    for text in ("thanks @evan.", "@evan: done", "ping @evan:", "hey @evan, ready?"):
        content = a._build_text_message_content(text)
        await a._resolve_bare_mentions("!room:example.org", content, text)
        assert content["m.mentions"]["user_ids"] == ["@evan:example.org"], text
        assert content["body"].count("matrix.to") == 1, text
    # the punctuation itself survives, attached OUTSIDE the link label
    content = a._build_text_message_content("thanks @evan.")
    await a._resolve_bare_mentions("!room:example.org", content, "thanks @evan.")
    assert content["body"] == "thanks [@evan](https://matrix.to/#/@evan:example.org)."


@pytest.mark.asyncio
async def test_hyphenated_and_dotted_longer_handles_are_not_partial_matched():
    a = _adapter(["@evan:example.org"])
    for text in ("ping @evan-smith now", "mail @evan.example.org instead"):
        content = a._build_text_message_content(text)
        await a._resolve_bare_mentions("!room:example.org", content, text)
        assert "m.mentions" not in content, text  # longer handle != this member
        assert content["body"] == text, text


@pytest.mark.asyncio
async def test_full_mxid_colon_still_excludes_bare_match():
    a = _adapter(["@evan:example.org"])
    text = "see @evan:example.org:8080 notes"  # host:port-shaped full MXID
    content = a._build_text_message_content(text)
    built_ids = list(content["m.mentions"]["user_ids"])  # builder quirk: swallows ':8080'
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert content["m.mentions"]["user_ids"] == built_ids  # bare resolver added nothing
    assert content["body"].count("matrix.to") == 0


@pytest.mark.asyncio
async def test_email_at_sign_is_not_a_handle():
    a = _adapter(["@evan:example.org"])
    text = "mail me at bob@evan.example please"
    content = a._build_text_message_content(text)
    await a._resolve_bare_mentions("!room:example.org", content, text)
    assert "m.mentions" not in content


@pytest.mark.asyncio
async def test_self_handle_never_self_mentions():
    a = _adapter(["@bot:example.org", "@evan:example.org"], mxid="@bot:example.org")
    content = a._build_text_message_content("@bot thinking")
    await a._resolve_bare_mentions("!room:example.org", content, "@bot thinking")
    assert "m.mentions" not in content


@pytest.mark.asyncio
async def test_member_lookup_failure_degrades_to_literal():
    a = _adapter([], fail=True)
    content = a._build_text_message_content("@evan ping")
    await a._resolve_bare_mentions("!room:example.org", content, "@evan ping")
    assert "m.mentions" not in content
    assert content["body"] == "@evan ping"


@pytest.mark.asyncio
async def test_member_lookup_is_cached_within_ttl():
    a = _adapter(["@evan:example.org"])
    for _ in range(3):
        content = a._build_text_message_content("@evan ping")
        await a._resolve_bare_mentions("!room:example.org", content, "@evan ping")
        assert content["m.mentions"]["user_ids"] == ["@evan:example.org"]
    assert a._client.calls == 1

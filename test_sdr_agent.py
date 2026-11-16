import asyncio
import json
import os
from contextlib import nullcontext
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
import requests
from agents.tool_context import ToolContext

import sdr_agent


ENV_NAMES = (
    "OPENAI_API_KEY",
    "RESEND_API_KEY",
    "RESEND_FROM",
    "EMAIL_ADDRESS",
    "EMAIL_APP_PASSWORD",
    "EMAIL_ADDRESS2",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MODEL_NAME",
    "SEND_TO_SELF",
)


@pytest.fixture(autouse=True)
def isolated_offline_config(monkeypatch, tmp_path):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEND_TO_SELF", "false")
    monkeypatch.setattr(sdr_agent, "DEALS_FILE", str(tmp_path / "deals.json"))
    monkeypatch.setattr(sdr_agent, "load_dotenv", lambda override=False: None)
    monkeypatch.setattr(sdr_agent, "trace", lambda *args, **kwargs: nullcontext())

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network call")

    monkeypatch.setattr(sdr_agent.requests, "post", no_network)


def make_deal(deal_id="acme-1234abcd", **overrides):
    deal = {
        "deal_id": deal_id,
        "prospect": {
            "first_name": "Ada",
            "company": "Acme",
            "role": "CTO",
            "email": "ada@example.com",
        },
        "stage": "outreach",
        "touches": 0,
        "history": [],
        "handled_ids": [],
        "opted_out": False,
    }
    deal.update(overrides)
    return deal


def store_deal(deal):
    deals = sdr_agent.load_deals()
    deals[deal["deal_id"]] = deal
    sdr_agent.save_deals(deals)


def reply(message_id="<m1>", text="Yes, tell me more", **overrides):
    value = {
        "uid": "7",
        "message_id": message_id,
        "deal_id": "acme-1234abcd",
        "from": "ADA@example.com",
        "subject": "Re: Hello [ref:acme-1234abcd]",
        "text": text,
    }
    value.update(overrides)
    return value


def tool_context(deal_id="acme-1234abcd", tool_name="send_reply", arguments="{}"):
    return ToolContext(
        sdr_agent.DealContext(deal_id),
        tool_name=tool_name,
        tool_call_id="call-1",
        tool_arguments=arguments,
    )


def invoke_tool(tool, arguments, deal_id="acme-1234abcd"):
    json_arguments = json.dumps(arguments)
    context = tool_context(
        deal_id=deal_id, tool_name=tool.name, arguments=json_arguments
    )
    return asyncio.run(tool.on_invoke_tool(context, json_arguments))


def test_extract_ref_matches_and_misses():
    assert sdr_agent.extract_ref("Re: Hello [ref:acme-1234abcd]") == "acme-1234abcd"
    assert sdr_agent.extract_ref("Hello") is None
    assert sdr_agent.extract_ref(None) is None


def test_send_and_log_blocks_touch_limit_without_resend(monkeypatch):
    store_deal(make_deal(touches=sdr_agent.MAX_TOUCHES))
    calls = []
    monkeypatch.setattr(sdr_agent, "send_resend", lambda *args: calls.append(args))

    result = sdr_agent.send_and_log("acme-1234abcd", "Subject", "Text", "<p>Text</p>")

    assert result.startswith("Blocked: touch limit")
    assert calls == []
    assert sdr_agent.load_deals()["acme-1234abcd"]["touches"] == sdr_agent.MAX_TOUCHES


def test_outbound_payload_self_recipient_single_ref_and_footer(monkeypatch):
    monkeypatch.setenv("SEND_TO_SELF", "yes")
    monkeypatch.setenv("EMAIL_ADDRESS2", "  OWNER@Example.COM ")
    monkeypatch.setenv("EMAIL_ADDRESS", " GMAIL@Example.COM ")
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "email-1"}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(sdr_agent.requests, "post", fake_post)

    sdr_agent.send_resend(
        "acme-1234abcd",
        "PROSPECT@Example.COM",
        "Hello [ref:old-id]",
        "Body",
        "<p>Body</p>",
    )

    assert captured["to"] == ["owner@example.com"]
    assert captured["reply_to"] == "gmail@example.com"
    assert captured["subject"] == "Hello [ref:acme-1234abcd]"
    assert captured["subject"].count("[ref:") == 1
    assert captured["text"].endswith('\n\nReply "stop" to opt out.')
    assert "Reply &quot;stop&quot; to opt out." in captured["html"]


def test_send_to_self_empty_recipient_raises_before_http(monkeypatch):
    monkeypatch.setenv("SEND_TO_SELF", "true")
    monkeypatch.setenv("EMAIL_ADDRESS2", "")
    calls = []
    monkeypatch.setattr(
        sdr_agent.requests, "post", lambda *args, **kwargs: calls.append(args)
    )

    with pytest.raises(RuntimeError, match="SEND_TO_SELF is on but EMAIL_ADDRESS2 is empty"):
        sdr_agent.send_resend("acme-1234abcd", "ada@example.com", "Hi", "Text", "HTML")

    assert calls == []


def test_unknown_sender_is_not_passed_to_closer(monkeypatch):
    store_deal(make_deal())
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(final_output="should not happen")

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)

    asyncio.run(sdr_agent.handle_reply(reply(**{"from": "attacker@example.net"})))

    assert calls == []
    stored = sdr_agent.load_deals()["acme-1234abcd"]
    assert stored["history"] == []
    assert stored["handled_ids"] == []


def test_closer_failure_retries_without_duplicate_history(monkeypatch):
    store_deal(make_deal())
    calls = []

    async def fake_run(agent, prompt, context=None):
        calls.append(context.deal_id)
        if len(calls) == 1:
            raise RuntimeError("closer failed")
        return SimpleNamespace(final_output="handled")

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)

    with pytest.raises(RuntimeError, match="closer failed"):
        asyncio.run(sdr_agent.handle_reply(reply()))
    first = sdr_agent.load_deals()["acme-1234abcd"]
    assert first["handled_ids"] == []
    assert len(first["history"]) == 1

    asyncio.run(sdr_agent.handle_reply(reply()))

    second = sdr_agent.load_deals()["acme-1234abcd"]
    assert calls == ["acme-1234abcd", "acme-1234abcd"]
    assert second["handled_ids"] == ["<m1>"]
    assert len(second["history"]) == 1
    assert second["history"][0]["message_id"] == "<m1>"


def test_identical_replies_with_different_message_ids_both_reach_closer(monkeypatch):
    store_deal(make_deal())
    calls = []

    async def fake_run(agent, prompt, context=None):
        calls.append(context.deal_id)
        return SimpleNamespace(final_output="handled")

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)

    asyncio.run(sdr_agent.handle_reply(reply("<one>", text="Yes")))
    asyncio.run(sdr_agent.handle_reply(reply("<two>", text="Yes")))

    stored = sdr_agent.load_deals()["acme-1234abcd"]
    assert len(calls) == 2
    assert stored["handled_ids"] == ["<one>", "<two>"]
    assert [item["message_id"] for item in stored["history"]] == ["<one>", "<two>"]


def test_stop_sets_lost_and_opted_out_without_email_or_closer(monkeypatch):
    store_deal(make_deal())
    calls = []
    monkeypatch.setattr(
        sdr_agent, "send_resend", lambda *args: calls.append(("send", args))
    )

    async def fake_run(*args, **kwargs):
        calls.append(("closer", args))
        return SimpleNamespace(final_output="should not happen")

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)

    asyncio.run(sdr_agent.handle_reply(reply(text="\n  Please STOP emailing me")))

    stored = sdr_agent.load_deals()["acme-1234abcd"]
    assert stored["stage"] == "lost"
    assert stored["opted_out"] is True
    assert stored["handled_ids"] == ["<m1>"]
    assert calls == []


def test_set_stage_rejects_won_and_cli_won_sets_it():
    store_deal(make_deal())

    result = invoke_tool(sdr_agent.set_stage, {"stage": "won"})

    assert result.startswith("Invalid stage")
    assert sdr_agent.load_deals()["acme-1234abcd"]["stage"] == "outreach"
    assert sdr_agent.main(["won", "acme-1234abcd"]) == 0
    assert sdr_agent.load_deals()["acme-1234abcd"]["stage"] == "won"


def test_cli_won_rejects_unknown_id(capsys):
    with pytest.raises(SystemExit) as exc_info:
        sdr_agent.main(["won", "missing-deal"])

    assert exc_info.value.code == 2
    assert "Unknown deal id: missing-deal" in capsys.readouterr().err


def test_notify_swallows_telegram_http_error(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")

    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(sdr_agent.requests, "post", fail)

    sdr_agent.notify("hello")

    assert "Telegram notification failed: ConnectionError" in capsys.readouterr().out


def test_closer_tools_use_context_and_have_no_deal_id_argument(monkeypatch):
    store_deal(make_deal())
    store_deal(make_deal("other-deadbeef"))
    monkeypatch.setattr(sdr_agent, "send_resend", lambda *args: "email-1")

    for tool in (sdr_agent.send_reply, sdr_agent.set_stage, sdr_agent.notify_human):
        assert "deal_id" not in tool.params_json_schema["properties"]

    sent = invoke_tool(
        sdr_agent.send_reply,
        {"subject": "Re: Hello", "text_body": "Thanks", "html_body": "<p>Thanks</p>"},
    )

    assert sent == "sent"
    deals = sdr_agent.load_deals()
    assert deals["acme-1234abcd"]["touches"] == 1
    assert deals["other-deadbeef"]["touches"] == 0


def test_final_handled_save_preserves_real_tool_send(monkeypatch):
    store_deal(make_deal())
    monkeypatch.setattr(sdr_agent, "send_resend", lambda *args: "email-1")

    async def fake_closer(agent, prompt, context=None):
        arguments = json.dumps(
            {"subject": "Re: Hello", "text_body": "Answer", "html_body": "<p>Answer</p>"}
        )
        await sdr_agent.send_reply.on_invoke_tool(
            tool_context(context.deal_id, arguments=arguments), arguments
        )
        return SimpleNamespace(final_output="answered")

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_closer)

    asyncio.run(sdr_agent.handle_reply(reply()))

    stored = sdr_agent.load_deals()["acme-1234abcd"]
    assert stored["touches"] == 1
    assert stored["handled_ids"] == ["<m1>"]
    assert [item["from"] for item in stored["history"]] == ["prospect", "us"]


def test_real_decorated_tool_propagates_resend_failure(monkeypatch):
    store_deal(make_deal())

    def fail_send(*args):
        raise requests.ConnectionError("Resend unavailable")

    monkeypatch.setattr(sdr_agent, "send_resend", fail_send)

    with pytest.raises(requests.ConnectionError, match="Resend unavailable"):
        invoke_tool(
            sdr_agent.send_reply,
            {"subject": "Re: Hello", "text_body": "Answer", "html_body": "<p>Answer</p>"},
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "won"},
        {"stage": "lost"},
        {"opted_out": True},
    ],
)
def test_send_boundary_blocks_closed_and_opted_out_deals(monkeypatch, overrides):
    store_deal(make_deal(**overrides))
    calls = []
    monkeypatch.setattr(sdr_agent, "send_resend", lambda *args: calls.append(args))

    result = sdr_agent.send_and_log("acme-1234abcd", "Subject", "Text", "HTML")

    assert result.startswith("Blocked:")
    assert calls == []


def test_start_deal_refuses_previously_opted_out_email(monkeypatch):
    store_deal(make_deal(opted_out=True, stage="lost"))
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(final_output=None)

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)

    with pytest.raises(ValueError, match="opted out"):
        asyncio.run(
            sdr_agent.start_deal(
                {
                    "first_name": "Ada",
                    "company": "Acme",
                    "role": "CTO",
                    "email": "  ADA@EXAMPLE.COM ",
                }
            )
        )

    assert calls == []


def test_start_deal_uses_uuid_suffix_and_normalizes_email(monkeypatch):
    draft = sdr_agent.Email(subject="Hello", text_body="Text", html_body="<p>Text</p>")

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(final_output=draft)

    monkeypatch.setattr(sdr_agent.Runner, "run", fake_run)
    monkeypatch.setattr(sdr_agent, "send_and_log", lambda *args: "sent")

    deal_id = asyncio.run(
        sdr_agent.start_deal(
            {
                "first_name": "Ada",
                "company": "Acme Labs!",
                "role": "CTO",
                "email": " ADA@EXAMPLE.COM ",
            }
        )
    )

    assert deal_id.startswith("acme-labs-")
    assert len(deal_id.rsplit("-", 1)[1]) == 8
    assert sdr_agent.load_deals()[deal_id]["prospect"]["email"] == "ada@example.com"


def test_save_deals_atomically_replaces_file(monkeypatch):
    calls = []
    real_replace = os.replace

    def recording_replace(source, destination):
        calls.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(sdr_agent.os, "replace", recording_replace)

    sdr_agent.save_deals({"acme-1234abcd": make_deal()})

    assert calls == [(f"{sdr_agent.DEALS_FILE}.tmp", sdr_agent.DEALS_FILE)]
    assert not os.path.exists(f"{sdr_agent.DEALS_FILE}.tmp")
    assert sdr_agent.load_deals()["acme-1234abcd"]["deal_id"] == "acme-1234abcd"


class FakeIMAP:
    def __init__(self, message_bytes):
        self.message_bytes = message_bytes
        self.calls = []
        self.logged_out = False

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"42"]
        if command == "fetch":
            return "OK", [(b"42", self.message_bytes)]
        if command == "store":
            return "OK", []
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


def email_bytes(html_only=False):
    message = EmailMessage()
    message["Subject"] = "Re: Hello [ref:acme-1234abcd]"
    message["From"] = "Ada <ADA@EXAMPLE.COM>"
    message["Message-ID"] = "<message-1@example.com>"
    if html_only:
        message.set_content(
            "<html><style>hidden</style><script>also hidden</script>"
            "<body>Hello &amp; thanks<br>next</body></html>",
            subtype="html",
        )
    else:
        message.set_content("Hello\nOn yesterday someone wrote:\nquoted")
    return message.as_bytes()


def test_fetch_replies_uses_peek_and_string_ids():
    imap = FakeIMAP(email_bytes())

    replies = sdr_agent.fetch_replies(imap)

    fetch = next(call for call in imap.calls if call[0] == "fetch")
    assert fetch[2] == "(BODY.PEEK[])"
    assert replies[0]["uid"] == "42"
    assert replies[0]["message_id"] == "<message-1@example.com>"
    assert replies[0]["from"] == "ada@example.com"
    assert replies[0]["text"] == "Hello"
    assert not any(call[0] == "store" for call in imap.calls)


def test_html_fallback_drops_script_style_tags_and_unescapes():
    imap = FakeIMAP(email_bytes(html_only=True))

    replies = sdr_agent.fetch_replies(imap)

    assert "hidden" not in replies[0]["text"]
    assert "Hello & thanks" in replies[0]["text"]


def test_fetch_replies_without_message_id_falls_back_to_uid():
    message = EmailMessage()
    message["Subject"] = "Hello [ref:acme-1234abcd]"
    message["From"] = "ada@example.com"
    message.set_content("Hello")
    imap = FakeIMAP(message.as_bytes())

    replies = sdr_agent.fetch_replies(imap)

    assert replies[0]["message_id"] == "42"


def test_run_cycle_isolates_poison_reply_and_closes_connection(monkeypatch):
    class CycleIMAP:
        def __init__(self):
            self.seen = []
            self.logged_out = False

        def uid(self, command, *args):
            if command == "store":
                self.seen.append(args[0])
            return "OK", []

        def logout(self):
            self.logged_out = True

    imap = CycleIMAP()
    replies = [reply("<bad>", uid="1"), reply("<good>", uid="2")]
    handled = []
    monkeypatch.setattr(sdr_agent, "open_imap", lambda: imap)
    monkeypatch.setattr(sdr_agent, "fetch_replies", lambda connection: replies)

    async def fake_handle(value):
        if value["uid"] == "1":
            raise RuntimeError("poison")
        handled.append(value["uid"])

    monkeypatch.setattr(sdr_agent, "handle_reply", fake_handle)

    asyncio.run(sdr_agent.run_cycle())

    assert handled == ["2"]
    assert imap.seen == ["2"]
    assert imap.logged_out is True


def test_missing_command_config_exits_two_before_work(monkeypatch, capsys):
    monkeypatch.setenv("SEND_TO_SELF", "true")
    called = []

    async def forbidden_start(*args):
        called.append(args)

    monkeypatch.setattr(sdr_agent, "start_deal", forbidden_start)

    with pytest.raises(SystemExit) as exc_info:
        sdr_agent.main(
            [
                "start",
                "--first-name",
                "Ada",
                "--company",
                "Acme",
                "--role",
                "CTO",
                "--email",
                "ada@example.com",
            ]
        )

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    for name in ("OPENAI_API_KEY", "RESEND_API_KEY", "EMAIL_ADDRESS", "EMAIL_ADDRESS2"):
        assert name in error
    assert called == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("STOP", True),
        ("Please unsubscribe me", True),
        ("do not contact me", True),
        ("Don't contact our team", True),
        ("remove me from this list", True),
        ("stopover at ours", False),
        ("Yes\nstop", False),
    ],
)
def test_opt_out_matching_uses_only_first_non_empty_line(text, expected):
    assert sdr_agent.is_opt_out(text) is expected

import argparse
import asyncio
import email as email_lib
import html as html_lib
import imaplib
import json
import os
import re
import uuid
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.policy import default as default_policy
from email.utils import parseaddr
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import requests
from agents import Agent, RunContextWrapper, Runner, function_tool, trace
from dotenv import load_dotenv
from pydantic import BaseModel


PRODUCT_PITCH = (
    "ComplAI is a SaaS tool for SOC2 compliance and audit preparation, powered by AI."
)
DEALS_FILE = "deals.json"
MAX_TOUCHES = 6
REPLY_WINDOW_DAYS = 14  # replies older than this are not fetched
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
VALID_STAGES = {"outreach", "negotiating", "won", "lost"}
AGENT_STAGES = {"negotiating", "lost"}
REF_RE = re.compile(r"\[ref:([\w-]+)\]")
OPT_OUT_RE = re.compile(
    r"\b(?:stop|unsubscribe|remove me|do not contact|don't contact)\b",
    re.IGNORECASE,
)
OPT_OUT_TEXT = '\n\nReply "stop" to opt out.'
OPT_OUT_HTML = '<p>Reply &quot;stop&quot; to opt out.</p>'


class Email(BaseModel):
    subject: str
    text_body: str
    html_body: str


@dataclass(frozen=True)
class Config:
    model_name: str
    resend_api_key: str
    resend_from: str
    email_address: str
    email_app_password: str
    self_recipient: str
    send_to_self: bool
    telegram_bot_token: str
    telegram_chat_id: str
    booking_link: str = ""


@dataclass(frozen=True)
class DealContext:
    deal_id: str


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def validated_booking_link(value: str) -> str:
    if not value:
        return ""
    valid = len(value) <= 200 and not any(
        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
        for char in value
    )
    try:
        parsed = urlsplit(value)
        valid = valid and (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        warnings.warn("Ignoring invalid BOOKING_LINK.", stacklevel=2)
        return ""
    return value


def get_config() -> Config:
    send_to_self = os.getenv("SEND_TO_SELF", "true").strip().lower() in {
        "true",
        "1",
        "yes",
    }
    return Config(
        model_name=os.getenv("MODEL_NAME", "gpt-5.4-mini"),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from=os.getenv("RESEND_FROM", "onboarding@resend.dev"),
        email_address=normalize_email(os.getenv("EMAIL_ADDRESS")),
        email_app_password=os.getenv("EMAIL_APP_PASSWORD", ""),
        self_recipient=normalize_email(os.getenv("EMAIL_ADDRESS2")),
        send_to_self=send_to_self,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        booking_link=validated_booking_link(os.getenv("BOOKING_LINK", "")),
    )


def extract_ref(subject: str | None) -> str | None:
    match = REF_RE.search(subject or "")
    return match.group(1) if match else None


def load_deals() -> dict[str, dict[str, Any]]:
    if os.path.exists(DEALS_FILE):
        with open(DEALS_FILE, encoding="utf-8") as file:
            return json.load(file)
    return {}


def save_deals(deals: dict[str, dict[str, Any]]) -> None:
    temporary_file = f"{DEALS_FILE}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(deals, file, indent=2)
        file.write("\n")
    os.replace(temporary_file, DEALS_FILE)


def update_deal(
    deal_id: str, update: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    deals = load_deals()
    if deal_id not in deals:
        raise KeyError(f"Unknown deal id: {deal_id}")
    deal = deals[deal_id]
    update(deal)
    save_deals(deals)
    return deal


def notify(text: str) -> None:
    config = get_config()
    if not (config.telegram_bot_token and config.telegram_chat_id):
        print("[telegram not set]", text)
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
            json={"chat_id": config.telegram_chat_id, "text": text[:4000]},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        error = str(exc).replace(config.telegram_bot_token, "[redacted]")
        print(f"Telegram notification failed: {type(exc).__name__}: {error}")


def send_resend(
    deal_id: str, to_addr: str, subject: str, text_body: str, html_body: str
) -> str:
    config = get_config()
    if config.send_to_self and not config.self_recipient:
        raise RuntimeError(
            "SEND_TO_SELF is on but EMAIL_ADDRESS2 is empty; "
            "set EMAIL_ADDRESS2 or disable SEND_TO_SELF."
        )
    recipient = (
        config.self_recipient if config.send_to_self else normalize_email(to_addr)
    )
    clean_subject = REF_RE.sub("", subject).strip()
    payload = {
        "from": config.resend_from,
        "to": [recipient],
        "subject": f"{clean_subject} [ref:{deal_id}]",
        "text": f"{text_body}{OPT_OUT_TEXT}",
        "html": f"{html_body}{OPT_OUT_HTML}",
        "reply_to": config.email_address,
    }
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {config.resend_api_key}"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


def send_and_log(deal_id: str, subject: str, text_body: str, html_body: str) -> str:
    deals = load_deals()
    if deal_id not in deals:
        raise KeyError(f"Unknown deal id: {deal_id}")
    deal = deals[deal_id]
    stage = deal.get("stage", "outreach")
    if stage in {"won", "lost"}:
        return f"Blocked: deal stage is {stage}."
    prospect_email = normalize_email(deal.get("prospect", {}).get("email"))
    if any(
        existing.get("opted_out", False)
        and normalize_email(existing.get("prospect", {}).get("email")) == prospect_email
        for existing in deals.values()
    ):
        return "Blocked: prospect has opted out."
    if deal.get("touches", 0) >= MAX_TOUCHES:
        return f"Blocked: touch limit of {MAX_TOUCHES} reached. Set the stage to lost instead."

    send_resend(
        deal_id,
        prospect_email,
        subject,
        text_body,
        html_body,
    )

    def record_send(current: dict[str, Any]) -> None:
        current["touches"] = current.get("touches", 0) + 1
        current.setdefault("history", []).append(
            {"from": "us", "subject": subject, "text": text_body}
        )

    updated = update_deal(deal_id, record_send)
    notify(
        f"Sent to {updated.get('prospect', {}).get('company', 'unknown')} "
        f"({deal_id}): {subject}"
    )
    return "sent"


def open_imap() -> imaplib.IMAP4_SSL:
    config = get_config()
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        imap.login(config.email_address, config.email_app_password)
        imap.select("INBOX")
    except Exception:
        try:
            imap.logout()
        finally:
            raise
    return imap


def mark_seen(uid: str, imap: imaplib.IMAP4_SSL) -> None:
    imap.uid("store", str(uid), "+FLAGS", "\\Seen")


def message_text(message: email_lib.message.Message) -> str:
    body = message.get_body(preferencelist=("plain",))
    if body is not None:
        text = body.get_content()
    else:
        body = message.get_body(preferencelist=("html",))
        if body is None:
            text = ""
        else:
            html_body = body.get_content()
            # shortcut: regex-based HTML cleanup is sufficient for a reply fallback.
            html_body = re.sub(
                r"<(script|style)\b[^>]*>.*?</\1\s*>",
                "",
                html_body,
                flags=re.IGNORECASE | re.DOTALL,
            )
            text = html_lib.unescape(re.sub(r"<[^>]+>", "", html_body))
    return re.split(r"\n(?=On .+wrote:|>)", text)[0].strip()


def imap_since(days: int, today: date | None = None) -> str:
    # IMAP needs English month names; strftime("%b") follows the OS locale.
    day = (today or date.today()) - timedelta(days=days)
    return f"{day.day:02d}-{IMAP_MONTHS[day.month - 1]}-{day.year}"


def fetch_replies(imap: imaplib.IMAP4_SSL | None = None) -> list[dict[str, str]]:
    owns_connection = imap is None
    if imap is None:
        imap = open_imap()
    try:
        # Read and unread both count: the owner may open a reply in Gmail first.
        # handled_ids, not the \Seen flag, decides what is already done.
        # shortcut: refetches every tagged message from the window each cycle; fine for
        # hundreds of threads, track the last seen UID if volume grows.
        _, data = imap.uid(
            "search", None, "SINCE", imap_since(REPLY_WINDOW_DAYS), "SUBJECT", "ref:"
        )
        replies = []
        for raw_uid in data[0].split():
            uid = raw_uid.decode("ascii") if isinstance(raw_uid, bytes) else str(raw_uid)
            _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            message = email_lib.message_from_bytes(
                msg_data[0][1], policy=default_policy
            )
            subject = str(
                make_header(decode_header(str(message["Subject"] or "")))
            )
            deal_id = extract_ref(subject)
            sender = normalize_email(parseaddr(str(message["From"]))[1])
            if sender == normalize_email(get_config().resend_from):
                mark_seen(uid, imap)
                continue
            if not deal_id:
                continue
            replies.append(
                {
                    "uid": uid,
                    "message_id": str(message["Message-ID"] or uid),
                    "deal_id": deal_id,
                    "from": sender,
                    "subject": subject,
                    "text": message_text(message),
                }
            )
        return replies
    finally:
        if owns_connection:
            imap.logout()


@function_tool(failure_error_function=None)
def send_reply(
    ctx: RunContextWrapper[DealContext],
    subject: str,
    text_body: str,
    html_body: str,
) -> str:
    """Send the next email in this deal's thread, under 120 words."""
    return send_and_log(ctx.context.deal_id, subject, text_body, html_body)


@function_tool(failure_error_function=None)
def set_stage(ctx: RunContextWrapper[DealContext], stage: str) -> str:
    """Set this deal's stage to negotiating or lost; only a human can set won."""
    stage = stage.strip().lower()
    if stage not in AGENT_STAGES:
        return "Invalid stage. Use negotiating or lost; only the human CLI can set won."

    def record_stage(deal: dict[str, Any]) -> None:
        deal["stage"] = stage

    deal = update_deal(ctx.context.deal_id, record_stage)
    if stage == "lost":
        notify(
            f"Deal lost: {deal.get('prospect', {}).get('company', 'unknown')} "
            f"({ctx.context.deal_id})."
        )
    return f"stage={stage}"


@function_tool(failure_error_function=None)
def notify_human(ctx: RunContextWrapper[DealContext], message: str) -> str:
    """Ask the human owner for help with pricing, legal, or uncertain requests."""
    notify(f"[{ctx.context.deal_id}] {message}")
    return "notified"


def make_outreach_writer() -> Agent:
    config = get_config()
    return Agent(
        name="Outreach Writer",
        model=config.model_name,
        output_type=Email,
        instructions=f"""
{PRODUCT_PITCH}
You write the first cold email. Address the prospect by first name, mention their company once,
and propose a short call. Under 120 words. Professional and warm.
Return subject, text_body and html_body.
""" + (
            f"Whenever you propose a call, include this exact booking link: {config.booking_link}\n"
            if config.booking_link else ""
        ),
    )


def make_closer() -> Agent:
    config = get_config()
    return Agent(
        name="SDR Closer",
        model=config.model_name,
        tools=[send_reply, set_stage, notify_human],
        instructions=f"""
{PRODUCT_PITCH}
You run the sales conversation. You get one deal as JSON and the prospect's latest reply.
The deal is bound to your run context; tools do not accept a deal_id.
Take exactly one next step:
- A question or interest: answer briefly and propose a 20-minute call. Use send_reply.
- An objection: acknowledge it, answer in 2-3 sentences, and propose a next step. Use send_reply.
- Pricing, contracts, or anything you cannot confirm: do not promise numbers. Use notify_human.
- A clear yes to buying: use set_stage with "negotiating", then call notify_human asking the owner to confirm the win.
- A clear no: use set_stage with "lost".
Never set won yourself. Emails are under 120 words. Use subject 'Re: <original subject>'.
Finish with a one-line summary of what you did.
""" + (
            f"Whenever you propose a call, include this exact booking link: {config.booking_link}\n"
            if config.booking_link else ""
        ),
    )


async def start_deal(prospect: dict[str, str]) -> str:
    prospect = dict(prospect)
    prospect["email"] = normalize_email(prospect.get("email"))
    for existing in load_deals().values():
        if existing.get("opted_out", False) and normalize_email(
            existing.get("prospect", {}).get("email")
        ) == prospect["email"]:
            raise ValueError("Cannot start a deal: this email address has opted out.")

    slug = re.sub(r"[^a-z0-9]+", "-", prospect["company"].lower()).strip("-")
    deal_id = f"{slug or 'deal'}-{uuid.uuid4().hex[:8]}"
    with trace(f"start deal {deal_id}"):
        result = await Runner.run(make_outreach_writer(), json.dumps(prospect))
    draft = result.final_output

    deals = load_deals()
    deals[deal_id] = {
        "deal_id": deal_id,
        "prospect": prospect,
        "stage": "outreach",
        "touches": 0,
        "history": [],
        "handled_ids": [],
        "opted_out": False,
    }
    save_deals(deals)
    print(send_and_log(deal_id, draft.subject, draft.text_body, draft.html_body))
    return deal_id


def is_opt_out(text: str) -> bool:
    first_non_empty = next(
        (line for line in text.splitlines() if line.strip()), ""
    )
    return OPT_OUT_RE.search(first_non_empty) is not None


async def handle_reply(reply: dict[str, str]) -> None:
    deal_id = reply["deal_id"]
    message_id = str(reply.get("message_id") or reply.get("uid") or "")
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        print("Ignoring reply for unknown deal", deal_id)
        return

    sender = normalize_email(reply.get("from"))
    prospect_email = normalize_email(deal.get("prospect", {}).get("email"))
    config = get_config()
    allowed_senders = {prospect_email}
    if config.send_to_self:
        allowed_senders.add(config.self_recipient)
    if sender not in allowed_senders:
        print(f"Ignoring reply from unknown sender for {deal_id}: {sender}")
        return

    handled_ids = deal.setdefault("handled_ids", [])
    if message_id in handled_ids:  # normal every cycle now that read mail is fetched too
        return

    if is_opt_out(reply.get("text", "")):
        def record_opt_out(current: dict[str, Any]) -> None:
            history = current.setdefault("history", [])
            if not any(item.get("message_id") == message_id for item in history):
                history.append(
                    {
                        "from": "prospect",
                        "message_id": message_id,
                        "subject": reply["subject"],
                        "text": reply["text"],
                    }
                )
            if current.get("stage") != "won":
                current["stage"] = "lost"
            current["opted_out"] = True
            handled = current.setdefault("handled_ids", [])
            if message_id not in handled:
                handled.append(message_id)

        updated = update_deal(deal_id, record_opt_out)
        notify(f"Opt-out recorded; deal {updated['stage']}: {deal_id}.")
        return

    if deal.get("stage", "outreach") in {"won", "lost"}:
        print("Ignoring reply for closed deal", deal_id)
        return

    history = deal.setdefault("history", [])
    if not any(item.get("message_id") == message_id for item in history):
        def record_reply(current: dict[str, Any]) -> None:
            current_history = current.setdefault("history", [])
            if not any(
                item.get("message_id") == message_id for item in current_history
            ):
                current_history.append(
                    {
                        "from": "prospect",
                        "message_id": message_id,
                        "subject": reply["subject"],
                        "text": reply["text"],
                    }
                )

        deal = update_deal(deal_id, record_reply)
        notify(
            f"Reply from {deal.get('prospect', {}).get('company', 'unknown')}:\n"
            f"{reply['text'][:500]}"
        )

    prompt = (
        f"Deal:\n{json.dumps(deal, indent=2)}\n\n"
        f"Latest prospect reply:\n{reply['text']}"
    )
    with trace(f"closer {deal_id}"):
        result = await Runner.run(
            make_closer(), prompt, context=DealContext(deal_id)
        )

    def record_handled(current: dict[str, Any]) -> None:
        current_handled = current.setdefault("handled_ids", [])
        if message_id not in current_handled:
            current_handled.append(message_id)

    update_deal(deal_id, record_handled)
    print(result.final_output)


async def run_cycle() -> None:
    imap = open_imap()
    try:
        replies = fetch_replies(imap)
        for reply in replies:
            try:
                await handle_reply(reply)
                mark_seen(reply["uid"], imap)
            except Exception as exc:
                print(
                    f"Failed to handle reply {reply['uid']} for "
                    f"{reply['deal_id']}: {exc!r}",
                    flush=True,
                )
    finally:
        imap.logout()
    print("Inbox checked.")


async def run_loop(interval_s: int = 60, cycles: int = 30) -> None:
    for _ in range(cycles):
        try:
            await run_cycle()
        except Exception as exc:
            print("cycle failed, retrying next tick:", repr(exc), flush=True)
        await asyncio.sleep(interval_s)


def confirm_won(deal_id: str) -> None:
    def record_win(deal: dict[str, Any]) -> None:
        deal["stage"] = "won"

    deal = update_deal(deal_id, record_win)
    notify(
        f"DEAL WON: {deal.get('prospect', {}).get('company', 'unknown')} "
        f"({deal_id}). Send the contract."
    )


def validate_config(command: str, config: Config) -> list[str]:
    if command not in {"start", "check", "loop"}:
        return []
    required = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "RESEND_API_KEY": config.resend_api_key,
        "EMAIL_ADDRESS": config.email_address,
    }
    if config.send_to_self:
        required["EMAIL_ADDRESS2"] = config.self_recipient
    if command in {"check", "loop"}:
        required["EMAIL_APP_PASSWORD"] = config.email_app_password
    return [name for name, value in required.items() if not value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ComplAI sales SDR agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a deal and send the first email")
    start.add_argument("--first-name", required=True)
    start.add_argument("--company", required=True)
    start.add_argument("--role", required=True)
    start.add_argument("--email", required=True)

    subparsers.add_parser("check", help="check Gmail once for replies")

    loop = subparsers.add_parser("loop", help="poll Gmail for replies")
    loop.add_argument("--interval", type=int, default=60)
    loop.add_argument("--cycles", type=int, default=30)

    won = subparsers.add_parser("won", help="let a human confirm a deal as won")
    won.add_argument("deal_id")

    subparsers.add_parser("deals", help="list stored deals")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(override=False)
    config = get_config()
    missing = validate_config(args.command, config)
    if missing:
        parser.exit(
            2,
            f"error: missing required environment variables for {args.command}: "
            f"{', '.join(missing)}\n",
        )

    if args.command == "start":
        deal_id = asyncio.run(
            start_deal(
                {
                    "first_name": args.first_name,
                    "company": args.company,
                    "role": args.role,
                    "email": args.email,
                }
            )
        )
        print(deal_id)
    elif args.command == "check":
        asyncio.run(run_cycle())
    elif args.command == "loop":
        asyncio.run(run_loop(interval_s=args.interval, cycles=args.cycles))
    elif args.command == "won":
        try:
            confirm_won(args.deal_id)
        except KeyError as exc:
            parser.exit(2, f"error: {exc.args[0]}\n")
    elif args.command == "deals":
        for deal_id, deal in load_deals().items():
            print(
                f"{deal_id} | {deal.get('prospect', {}).get('company', 'unknown')} "
                f"| {deal.get('stage', 'outreach')} "
                f"| touches={deal.get('touches', 0)}"
            )
    return 0


if __name__ == "__main__":
    main()

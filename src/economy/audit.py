"""The administrative events a bank reports to its audit channel.

A bank's leaders may choose one channel (Discord) or group/topic (Telegram)
in /bank edit; this module decides what goes there. Only administrative
operations do: fines (`fine`), emissions (`emit`), the monthly wages and
party dues paid in the currency (`wages`, `dues`), changes of the
conversion fee and the bank tax (`fee`, `tax`), the parts of the economy
switched off or on for the currency (`econ_off`, `econ_on`), and changes of leadership
(`leader_added`, `leader_transfer`). Ordinary payments, sales and earnings
never do — they are the members' own business, and PRIVACY.md promises
that balances are not published.

`record` only queues the event (db/audit.py), with its values already
formatted — except `part`, a part's code (economy/switches.py), which is
named in the channel's language when posted — and only when the bank has a
channel; main.py: audit_loop posts
it in the channel's language as `audit_<event>`. The channel is stored as
'guild:channel' on Discord — the guild is what the channel's language is
resolved through — and as 'chat:topic' on Telegram.

Not this module's zone: the posting loop (main.py) and the delivery to a
platform (stats.py: post_to_channel).
"""
import db

EVENTS = ("fine", "emit", "wages", "dues", "fee", "tax", "econ_off", "econ_on",
          "leader_added", "leader_transfer")

def record(bank_code, event, **fields):
    """Queue an administrative event of a bank for its audit channel. Does
    nothing when the bank has no channel."""
    bank = db.get_bank(bank_code)
    if not bank or not bank["audit_channel"]:
        return
    db.enqueue_audit(bank_code, event, {k: "" if v is None else str(v) for k, v in fields.items()})

async def flush():
    """Post every queued event to its bank's channel and drop it. An event
    whose bank or channel is gone, or that could not be delivered for a
    day, is dropped too; anything else is retried on the next pass.
    Returns how many were posted."""
    import time
    import stats
    from utils import localized
    posted = 0
    for item in db.due_audits():
        bank = db.get_bank(item["bank_code"])
        if not bank or not bank["audit_channel"]:
            db.delete_audit(item["id"])
            continue
        platform, key = bank["audit_platform"], bank["audit_channel"]
        server, _, channel = key.partition(":")
        if platform == "discord":
            row = {"platform": platform, "server_id": server, "channel_key": channel}
            target = channel
        else:
            row = {"platform": platform, "server_id": server, "channel_key": key}
            target = key
        lang = stats.channel_lang(row)
        fields = dict(item["fields"])
        if "part" in fields:
            from economy.switches import Part
            fields["part"] = Part(fields["part"])
        text = localized(f"audit_{item['event']}", lang,
                         bank=f"{bank['currency_name']} [{bank['code']}]", **fields)
        try:
            await stats.post_to_channel(platform, target, localized("audit_title", lang), [text])
            db.delete_audit(item["id"])
            posted += 1
        except Exception:
            if time.time() - int(item["created_at"] or 0) > 86400:
                db.delete_audit(item["id"])
    return posted

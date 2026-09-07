"""Shared rolling-window scheduling; these are user policy, not spam guarantees."""
import random
import time
from local_store import MailError


PROFILES = {
    "gmail": {"recipients_per_day": 50, "messages_per_window": 6, "window_seconds": 3600,
              "gap_min_seconds": 600, "gap_max_seconds": 600},
    "exchange": {"recipients_per_day": 50, "messages_per_window": 6, "window_seconds": 3600,
                 "gap_min_seconds": 600, "gap_max_seconds": 600},
}


class WaitRequired(MailError):
    def __init__(self, until, reason):
        super().__init__(reason)
        self.until = until
        self.reason = reason


class SuppressedRecipient(MailError):
    pass


def profile(account):
    return account.get("outreach_policy", PROFILES["exchange" if account["kind"] == "com.apple.account.Exchange" else "gmail"]).copy()


def initialise(connection):
    columns = {r[1] for r in connection.execute("pragma table_info(sends)")}
    if "purpose" not in columns:
        connection.execute("alter table sends add column purpose text not null default 'personal'")
    if "owner_pid" not in columns:
        connection.execute("alter table sends add column owner_pid integer")
    if "owner_start" not in columns:
        connection.execute("alter table sends add column owner_start text")
    if "logical_item" not in columns:
        connection.execute("alter table sends add column logical_item text")
    connection.execute("""create table if not exists pacing(account text primary key,next_at real not null)""")
    if "gap_seconds" not in {r[1] for r in connection.execute("pragma table_info(pacing)")}:
        connection.execute("alter table pacing add column gap_seconds real not null default 0")
    connection.execute("""create table if not exists suppressions(address text primary key,reason text not null,created real not null)""")
    connection.execute("""create table if not exists account_holds(account text primary key,reason text,created real)""")


def eligibility(events, *, now, recipients, rules, next_at=0):
    """Return the earliest eligible wall-clock time and the limiting reason."""
    if recipients > rules["recipients_per_day"]:
        raise MailError("This message alone exceeds the account's daily outreach recipient cap")
    allowed, reason = max(now, next_at), "minimum gap" if next_at > now else "ready"
    daily = sorted((stamp, count) for stamp, count in events if stamp > now - 86400)
    used = sum(count for _, count in daily)
    remaining = used
    for stamp, count in daily:
        if remaining + recipients <= rules["recipients_per_day"]:
            break
        remaining -= count
        deadline = stamp + 86400
        if deadline > allowed:
            allowed, reason = deadline, "rolling 24-hour recipient cap"
    recent = sorted(stamp for stamp, _ in events if stamp > now - rules["window_seconds"])
    cap = rules["messages_per_window"]
    if len(recent) >= cap:
        deadline = recent[len(recent) - cap] + rules["window_seconds"]
        if deadline > allowed:
            allowed, reason = deadline, "rolling message-rate cap"
    return allowed, reason


def reserve(connection, account, recipients, addresses, purpose, *, now=None):
    """Called inside the same BEGIN IMMEDIATE transaction as the send reservation."""
    now = time.time() if now is None else now
    # Published provider ceilings apply to all amail sends, including personal
    # replies. This ledger cannot see every other client or tenant-level limit.
    all_events = connection.execute("select created,recipients from sends where account=? and state!='rejected' and created>? order by created",
                                    (account["uuid"], now - 86400)).fetchall()
    if recipients > 500:
        raise MailError("A message exceeds the configured 500-recipient provider ceiling")
    if account["kind"] == "com.apple.account.Exchange":
        until, reason = eligibility(all_events, now=now, recipients=recipients,
            rules={"recipients_per_day":10000,"messages_per_window":30,"window_seconds":60})
    else:
        until, reason = eligibility([(stamp,1) for stamp,_ in all_events], now=now, recipients=1,
            rules={"recipients_per_day":500,"messages_per_window":500,"window_seconds":86400})
    if until > now:
        raise WaitRequired(until, "provider ceiling: " + reason)
    if purpose != "outreach":
        return
    hold = connection.execute("select reason from account_holds where account=?", (account["uuid"],)).fetchone()
    if hold:
        raise MailError("Outreach is paused for this account: " + hold[0])
    for value in addresses:
        suppressed = connection.execute("select reason from suppressions where address=?", (value.casefold(),)).fetchone()
        if suppressed:
            raise SuppressedRecipient(f"Recipient {value} is suppressed: {suppressed[0]}")
    events = connection.execute("""select created,recipients from sends where account=? and purpose='outreach'
                    and state!='rejected' and created>? order by created""", (account["uuid"], now - 86400)).fetchall()
    next_row = connection.execute("select next_at from pacing where account=?", (account["uuid"],)).fetchone()
    rules = profile(account)
    until, reason = eligibility(events, now=now, recipients=recipients, rules=rules,
                                next_at=next_row[0] if next_row else 0)
    if until > now:
        raise WaitRequired(until, reason)
    gap = random.SystemRandom().uniform(rules["gap_min_seconds"], rules["gap_max_seconds"])
    connection.execute("insert or replace into pacing values(?,?,?)", (account["uuid"], now + gap, gap))


def usage(connection, account, *, now=None):
    now = time.time() if now is None else now
    rules = profile(account)
    events = connection.execute("""select created,recipients from sends where account=? and purpose='outreach'
                 and state!='rejected' and created>? order by created""", (account["uuid"], now - 86400)).fetchall()
    next_row = connection.execute("select next_at from pacing where account=?", (account["uuid"],)).fetchone()
    eligible, reason = eligibility(events, now=now, recipients=1, rules=rules,
                                   next_at=next_row[0] if next_row else 0)
    provider = ({"recipients_per_24h":10000,"messages_per_minute":30,"default_recipients_per_message":500,
                 "source":"https://learn.microsoft.com/en-us/defender-office-365/outbound-spam-sending-limits-troubleshoot"}
                if account["kind"] == "com.apple.account.Exchange" else
                {"messages_per_day":500,"recipients_per_message":500,"source":"https://support.google.com/mail/answer/22839?hl=en"})
    provider.update(checked_at="2026-09-07",additional_account_or_tenant_restrictions="unknown")
    return {"account": account["name"], "policy": rules, "provider_limits":provider, "outreach_recipients_24h": sum(r[1] for r in events),
            "messages_in_rate_window": sum(r[0] > now - rules["window_seconds"] for r in events),
            "next_eligible_at": eligible, "reason": reason,
            "coverage": "amail ledger; other clients and tenant-wide activity may consume additional provider allowance"}

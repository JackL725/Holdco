"""Company mailboxes: send with SMTP, receive with IMAP (standard library only)."""
from __future__ import annotations

import datetime as _dt
import email
import email.policy
import email.utils
import imaplib
import smtplib
import ssl
from email.message import EmailMessage

from . import bus, db, web


class EmailError(Exception):
    pass


def smtp_configured(cs: dict) -> bool:
    return bool(cs.get("smtp_host") and cs.get("smtp_user") and cs.get("smtp_password"))


def imap_configured(cs: dict) -> bool:
    return bool(cs.get("imap_host") and cs.get("imap_user") and cs.get("imap_password"))


def send(company: dict, to: str, subject: str, body: str, in_reply_to: str = "") -> str:
    cs = db.company_settings(company)
    if not smtp_configured(cs):
        raise EmailError("Email isn't set up for this company. Add SMTP details in the company's Settings tab.")
    sender = cs.get("smtp_from") or cs["smtp_user"]
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((cs.get("smtp_from_name") or company["name"], sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    port = int(cs.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(cs["smtp_host"], port, context=ctx, timeout=30) as s:
                s.login(cs["smtp_user"], cs["smtp_password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cs["smtp_host"], port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(cs["smtp_user"], cs["smtp_password"])
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"Sending failed: {e}") from None
    db.run("INSERT INTO emails(company_id, direction, message_id, from_addr, to_addr, subject, body, status, created_at)"
           " VALUES(?,?,?,?,?,?,?,?,?)",
           (company["id"], "out", msg["Message-ID"], sender, to, subject, body, "sent", db.now()))
    return f"Email sent to {to}."


def _body_text(msg) -> str:
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = content
            elif ctype == "text/html" and html is None:
                html = content
        if plain:
            return plain
        if html:
            return web.html_to_text(html)[1]
        return ""
    try:
        content = msg.get_content()
    except Exception:
        return ""
    if msg.get_content_type() == "text/html":
        return web.html_to_text(content)[1]
    return content if isinstance(content, str) else ""


def poll_inbox(company: dict) -> int:
    """Fetch new messages into the emails table. Returns how many were new."""
    cs = db.company_settings(company)
    if not imap_configured(cs):
        return 0
    last_uid = db.scalar("SELECT MAX(CAST(uid AS INTEGER)) FROM emails WHERE company_id=? AND direction='in'",
                         (company["id"],)) or 0
    new = 0
    try:
        with imaplib.IMAP4_SSL(cs["imap_host"], int(cs.get("imap_port") or 993)) as M:
            M.login(cs["imap_user"], cs["imap_password"])
            M.select("INBOX", readonly=True)
            if last_uid:
                typ, data = M.uid("search", None, f"UID {int(last_uid) + 1}:*")
            else:
                since = (_dt.date.today() - _dt.timedelta(days=3)).strftime("%d-%b-%Y")
                typ, data = M.uid("search", None, f"SINCE {since}")
            uids = [u for u in (data[0] or b"").split() if int(u) > int(last_uid)]
            for uid in uids[-50:]:
                typ, msg_data = M.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                body = _body_text(msg)[:20000]
                eid = db.run(
                    "INSERT INTO emails(company_id, direction, uid, message_id, from_addr, to_addr, subject, body,"
                    " status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (company["id"], "in", uid.decode(), str(msg.get("Message-ID", "")), str(msg.get("From", "")),
                     str(msg.get("To", "")), str(msg.get("Subject", "")), body, "new", db.now()))
                new += 1
                bus.emit(company["id"], "email", f"New email from {msg.get('From', '?')}: {msg.get('Subject', '')}",
                         data={"email_id": eid})
                _route_inbound(company, eid, str(msg.get("From", "")), str(msg.get("Subject", "")))
    except (imaplib.IMAP4.error, OSError) as e:
        bus.emit(company["id"], "error", f"Checking the inbox failed: {e}")
    return new


def _route_inbound(company: dict, email_id: int, sender: str, subject: str) -> None:
    agent = (db.one("SELECT * FROM agents WHERE company_id=? AND role='support' AND enabled=1", (company["id"],))
             or db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (company["id"],)))
    if not agent:
        return
    ts = db.now()
    db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, created_by,"
           " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (company["id"], f"Reply to email #{email_id} from {sender[:60]}",
            f"New inbound email #{email_id}. Subject: {subject}\nRead it with check_inbox, reply with send_email "
            f"(reply_to_email_id={email_id}) if a reply is needed, then mark_email_handled.",
            agent["role"], agent["id"], "todo", 1, "inbox", ts, ts))

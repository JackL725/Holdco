"""Company mailboxes: send with SMTP or Resend, receive with IMAP (standard library only).

The company setting email_provider picks the sender (Phase 1): smtp, resend, or auto (Resend when the company has
an enabled Resend connection whose last health check was ok, else SMTP). Inbound mail is always IMAP.
"""
from __future__ import annotations

import datetime as _dt
import email
import email.policy
import email.utils
import hashlib
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


def _resend_instance(company: dict, cs: dict) -> dict | None:
    """The Resend connection to send through, or None for SMTP. Raises EmailError when the owner chose resend and
    there is no enabled Resend connection."""
    choice = str(cs.get("email_provider") or "auto").strip().lower()
    if choice == "smtp":
        return None
    try:
        from . import connectors   # imported here: connectors imports tools, which imports this module
        inst = connectors.instance("resend", company)
    except Exception as e:   # the framework failing must not stop SMTP mail
        if choice == "resend":
            raise EmailError(f"Resend isn't available: {e}") from None
        return None
    usable = bool(inst and inst.get("enabled", 1))
    if choice == "resend":
        if not usable:
            raise EmailError("email_provider is resend, but Resend isn't connected for this company. Set it up in "
                             "the company's Settings > Connectors, or set email_provider to auto.")
        return inst
    return inst if usable and inst.get("status") == "ok" else None


def _idempotency_key(company: dict, to: str, subject: str, body: str) -> str:
    """Resend's Idempotency-Key, so a retried approval cannot send twice. The approval id when this send runs one
    (actions.decide marks the row 'running' before executing it), plus a hash of the content so an edited retry is
    a new request; a send without an approval row uses the hash of company, recipient, subject and body alone."""
    digest = hashlib.sha256(f"{company['id']}\n{to}\n{subject}\n{body}".encode("utf-8")).hexdigest()
    for row in db.q("SELECT id, payload FROM approvals WHERE company_id=? AND action='send_email' AND status='running' "
                    "ORDER BY id DESC LIMIT 20", (company["id"],)):
        p = db.loads(row["payload"], {}) or {}
        if p.get("to") == to and p.get("subject") == subject and p.get("body") == body:
            return f"holdco-approval-{row['id']}-{digest[:16]}"
    return f"holdco-{digest[:40]}"


def _reply_address(cs: dict, sender: str) -> str:
    """Replies to Resend mail go to the mailbox Holdco reads over IMAP, when that is a different address."""
    for candidate in (cs.get("imap_user"), cs.get("smtp_from"), cs.get("smtp_user")):
        c = str(candidate or "").strip()
        if "@" in c:
            return "" if c.lower() == sender.lower() else c
    return ""


def _send_resend(company: dict, cs: dict, inst: dict, to: str, subject: str, body: str, in_reply_to: str,
                 idempotency_key: str) -> str:
    from . import conn_resend
    sender = conn_resend.from_address(inst)
    if "@" not in sender:
        raise EmailError("Resend needs a from address: add it on the Resend card in the company's Settings.")
    msgid = email.utils.make_msgid(domain=sender.split("@")[-1])
    headers = {"Message-ID": msgid}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = in_reply_to
    try:
        rid = conn_resend.send(
            inst, company, sender=email.utils.formataddr((conn_resend.from_name(inst) or company["name"], sender)),
            to=to, subject=subject, text=body, reply_to=_reply_address(cs, sender), headers=headers,
            idempotency_key=idempotency_key or _idempotency_key(company, to, subject, body))
    except conn_resend.ResendError as e:
        raise EmailError(str(e)) from None
    # Holdco's Message-ID stays in message_id (replies are matched on it); Resend's id goes in uid.
    db.run("INSERT INTO emails(company_id, direction, uid, message_id, from_addr, to_addr, subject, body, status,"
           " created_at, in_reply_to) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (company["id"], "out", rid, msgid, sender, to, subject, body, "sent", db.now(), in_reply_to or ""))
    return f"Email sent to {to} (through Resend)."


def send(company: dict, to: str, subject: str, body: str, in_reply_to: str = "", idempotency_key: str = "") -> str:
    """Send one email through Resend or SMTP (email_provider) and record it. idempotency_key (Resend only) defaults
    to the running approval's id, else a hash of the message (_idempotency_key)."""
    cs = db.company_settings(company)
    inst = _resend_instance(company, cs)
    if inst is not None:
        return _send_resend(company, cs, inst, to, subject, body, in_reply_to, idempotency_key)
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
    db.run("INSERT INTO emails(company_id, direction, message_id, from_addr, to_addr, subject, body, status,"
           " created_at, in_reply_to) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (company["id"], "out", msg["Message-ID"], sender, to, subject, body, "sent", db.now(), in_reply_to or ""))
    return f"Email sent to {to}."


TEST_TIMEOUT = 8   # seconds per socket operation in the login tests (the Doctor caps a whole check at 15 s)


def test_smtp(cs: dict) -> str:
    """Prove the SMTP details work: connect, STARTTLS (or SSL on 465), log in, quit. Nothing is sent.
    Raises EmailError with the server's reason; the password never appears in it."""
    if not smtp_configured(cs):
        raise EmailError("SMTP is not configured (host, user and password are all needed).")
    host, user = cs["smtp_host"], cs["smtp_user"]
    port = int(cs.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TEST_TIMEOUT) as s:
                s.login(user, cs["smtp_password"])
        else:
            with smtplib.SMTP(host, port, timeout=TEST_TIMEOUT) as s:
                s.starttls(context=ctx)
                s.login(user, cs["smtp_password"])
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"SMTP login as {user} at {host}:{port} failed: {e}") from None
    return f"SMTP login ok as {user} at {host}:{port}."


def test_imap(cs: dict) -> str:
    """Prove the IMAP details work: connect over SSL, log in, log out. Raises EmailError on failure."""
    if not imap_configured(cs):
        raise EmailError("IMAP is not configured (host, user and password are all needed).")
    host, user = cs["imap_host"], cs["imap_user"]
    port = int(cs.get("imap_port") or 993)
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=TEST_TIMEOUT)
        try:
            M.login(user, cs["imap_password"])
        finally:
            try:
                M.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        raise EmailError(f"IMAP login as {user} at {host}:{port} failed: {e}") from None
    return f"IMAP login ok as {user} at {host}:{port}."


# Names used by the phase document; same functions.
check_smtp = test_smtp
check_imap = test_imap


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
                    " status, created_at, in_reply_to) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (company["id"], "in", uid.decode(), str(msg.get("Message-ID", "")), str(msg.get("From", "")),
                     str(msg.get("To", "")), str(msg.get("Subject", "")), body, "new", db.now(), _in_reply_to(msg)))
                new += 1
                bus.emit(company["id"], "email", f"New email from {msg.get('From', '?')}: {msg.get('Subject', '')}",
                         data={"email_id": eid})
                _route_inbound(company, eid, str(msg.get("From", "")), str(msg.get("Subject", "")))
    except (imaplib.IMAP4.error, OSError) as e:
        bus.emit(company["id"], "error", f"Checking the inbox failed: {e}")
    return new


def _in_reply_to(msg) -> str:
    """The Message-ID an inbound email answers: its In-Reply-To header, else the last id in References."""
    value = " ".join(str(msg.get("In-Reply-To", "") or "").split())
    if not value:
        refs = str(msg.get("References", "") or "").split()
        value = refs[-1] if refs else ""
    return value[:998]


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

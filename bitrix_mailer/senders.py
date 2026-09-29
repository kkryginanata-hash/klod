"""Отправка письма и фиксация его в Timeline существующей CRM-сущности.

* ``bitrix`` (по умолчанию) — письмо создаётся как дело «E-mail» (crm.activity.add,
  TYPE_ID=4, DIRECTION=2) у сделки с получателем-контактом. Битрикс24 сам
  отправляет его через подключённый к CRM ящик, и в Timeline сделки и контакта
  появляется настоящее исходящее письмо.
* ``smtp`` — письмо уходит через ваш SMTP-сервер, а в Timeline сделки (и, по
  желанию, контакта) пишется комментарий с темой, адресатом и текстом письма.
  Настоящее «исходящее письмо» через вебхук без отправки создать нельзя, поэтому
  этот режим — запасной.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .client import ACTIVITY_EMAIL, DIRECTION_OUTGOING, ENTITY_CONTACT, ENTITY_DEAL, BitrixClient
from .template import html_to_text


@dataclass
class Message:
    deal_id: int
    contact_id: int
    contact_name: str
    email: str
    subject: str
    html: str
    responsible_id: int | None = None


@dataclass
class SendResult:
    timeline_id: str
    verified: bool
    note: str = ""


class BitrixEmailSender:
    name = "bitrix"

    def __init__(self, client: BitrixClient, from_address: str, responsible_id: int | None = None):
        self.client = client
        self.from_address = from_address
        self.responsible_id = responsible_id

    def send(self, m: Message) -> SendResult:
        fields = {
            "OWNER_TYPE_ID": ENTITY_DEAL,
            "OWNER_ID": m.deal_id,
            "TYPE_ID": ACTIVITY_EMAIL,
            "DIRECTION": DIRECTION_OUTGOING,
            "SUBJECT": m.subject,
            "DESCRIPTION": m.html,
            "DESCRIPTION_TYPE": 3,  # HTML
            "COMPLETED": "Y",
            "COMMUNICATIONS": [{"VALUE": m.email, "ENTITY_ID": m.contact_id, "ENTITY_TYPE_ID": ENTITY_CONTACT}],
            "SETTINGS": {"MESSAGE_FROM": self.from_address},
        }
        responsible = self.responsible_id or m.responsible_id
        if responsible:
            fields["RESPONSIBLE_ID"] = responsible
        activity_id = str(self.client.call("crm.activity.add", {"fields": fields}))
        return SendResult(activity_id, *self.verify(activity_id, m))

    def verify(self, activity_id: str, m: Message) -> tuple[bool, str]:
        """Письмо есть в Timeline сделки как исходящее и адресовано нужному контакту."""
        a = self.client.call("crm.activity.get", {"id": activity_id}) or {}
        problems = []
        if str(a.get("TYPE_ID")) != str(ACTIVITY_EMAIL):
            problems.append(f"TYPE_ID={a.get('TYPE_ID')}")
        if str(a.get("DIRECTION")) != str(DIRECTION_OUTGOING):
            problems.append(f"DIRECTION={a.get('DIRECTION')}")
        if str(a.get("OWNER_TYPE_ID")) != str(ENTITY_DEAL) or str(a.get("OWNER_ID")) != str(m.deal_id):
            problems.append(f"привязано к {a.get('OWNER_TYPE_ID')}:{a.get('OWNER_ID')}")
        comms = a.get("COMMUNICATIONS") or []
        if comms and not any(str(c.get("ENTITY_ID")) == str(m.contact_id) for c in comms):
            problems.append("получатель не совпадает с контактом")
        return (not problems, "; ".join(problems))


class SmtpSender:
    name = "smtp"

    def __init__(self, client: BitrixClient, *, host: str, port: int, username: str, password: str,
                 from_address: str, from_name: str = "", security: str = "ssl",
                 reply_to: str = "", log_to_contact: bool = True):
        self.client = client
        self.host, self.port, self.username, self.password = host, port, username, password
        self.from_address, self.from_name, self.security = from_address, from_name, security
        self.reply_to = reply_to
        self.log_to_contact = log_to_contact
        self._smtp: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        if self._smtp is None:
            ctx = ssl.create_default_context()
            if self.security == "ssl":
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=60)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=60)
                if self.security == "starttls":
                    smtp.starttls(context=ctx)
            if self.username:
                smtp.login(self.username, self.password)
            self._smtp = smtp
        return self._smtp

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except smtplib.SMTPException:
                pass
            self._smtp = None

    def send(self, m: Message) -> SendResult:
        msg = EmailMessage()
        msg["From"] = formataddr((self.from_name, self.from_address)) if self.from_name else self.from_address
        msg["To"] = formataddr((m.contact_name, m.email)) if m.contact_name else m.email
        msg["Subject"] = m.subject
        msg["Message-ID"] = make_msgid(domain=Address(addr_spec=self.from_address).domain)
        msg["X-CRM-Deal-ID"] = str(m.deal_id)
        msg["X-CRM-Contact-ID"] = str(m.contact_id)
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg.set_content(html_to_text(m.html))
        msg.add_alternative(m.html, subtype="html")
        try:
            self._connect().send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused):
            self.close()
            self._connect().send_message(msg)

        comment = (
            f"[B]Исходящее письмо[/B] → {m.contact_name} <{m.email}>\n"
            f"Тема: {m.subject}\nMessage-ID: {msg['Message-ID']}\n\n{html_to_text(m.html)}"
        )
        cid = str(self.client.call("crm.timeline.comment.add",
                                   {"fields": {"ENTITY_TYPE": "deal", "ENTITY_ID": m.deal_id, "COMMENT": comment}}))
        if self.log_to_contact:
            self.client.call("crm.timeline.comment.add",
                             {"fields": {"ENTITY_TYPE": "contact", "ENTITY_ID": m.contact_id, "COMMENT": comment}})
        got = self.client.call("crm.timeline.comment.get", {"id": cid}) or {}
        ok = str(got.get("ENTITY_ID")) == str(m.deal_id)
        return SendResult(cid, ok, "" if ok else "комментарий не найден в Timeline сделки")


class DryRunSender:
    """Ничего не отправляет и не пишет в CRM — для проверки сценария."""

    name = "dry-run"

    def __init__(self):
        self.sent: list[Message] = []

    def send(self, m: Message) -> SendResult:
        self.sent.append(m)
        return SendResult(f"dry-{uuid.uuid4().hex[:8]}", True, "dry-run")


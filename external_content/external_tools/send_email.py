"""External tool: send an email via SMTP.

Lets Reachy compose and send an email on the user's behalf (e.g. a reminder,
a summary of the conversation, a note to a colleague).

Configuration is read from environment variables so no secrets live in code:

    SMTP_HOST          SMTP server host (e.g. smtp.gmail.com)
    SMTP_PORT          SMTP server port (default 587 for STARTTLS, 465 for SSL)
    SMTP_USERNAME      Username for SMTP auth (often the same as EMAIL_FROM)
    SMTP_PASSWORD      Password or app-specific password for SMTP auth
    EMAIL_FROM         The "From" address (defaults to SMTP_USERNAME if unset)
    EMAIL_DEFAULT_TO   Optional fallback recipient when the model omits `to`
    SMTP_USE_SSL       Set to "1"/"true" to use implicit SSL instead of STARTTLS
    CONTACTS_FILE      Optional path to the contacts JSON file (name -> email).
                       Defaults to contacts.json next to this tool.

The contact list (contacts.json) is embedded into the tool description so the
model itself reads it and chooses the right email when the user names a person
("email Cote") — no name matching is done in code. The `to` argument must be
an actual email address.

Enable it by adding `send_email` to a profile's tools.txt, or by setting
AUTOLOAD_EXTERNAL_TOOLS=1 with REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY pointing
at this folder.
"""

import asyncio
import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

_DEFAULT_CONTACTS_PATH = Path(__file__).with_name("contacts.json")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_contacts() -> Dict[str, str]:
    """Load the name -> email contacts map, keyed lowercase for lookup."""
    path = Path(os.environ.get("CONTACTS_FILE", str(_DEFAULT_CONTACTS_PATH)))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read contacts file at %s", path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Contacts file %s is not a JSON object; ignoring", path)
        return {}
    # Preserve the original-case names; the model reads these to choose a recipient.
    return {str(name).strip(): str(email).strip() for name, email in raw.items()}


def _contacts_description() -> str:
    """Render the contact list as text for the tool description.

    The model reads this list and picks the correct email itself — no
    name-matching is done in code.
    """
    contacts = _load_contacts()
    if not contacts:
        return ""
    lines = "\n".join(f"  - {name}: {email}" for name, email in contacts.items())
    return (
        "\n\nWhen the user refers to a recipient by name, choose the matching "
        "email address from this contact list and pass it as `to`:\n" + lines
    )


def _send_email_blocking(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_ssl: bool,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """Build and send the message synchronously (run via asyncio.to_thread)."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            if username and password:
                server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(msg)


class SendEmail(Tool):
    """Send an email with arbitrary subject and body via SMTP."""

    name = "send_email"
    description = (
        "Send an email on the user's behalf. Use this to deliver a written "
        "message, reminder, summary, or note. Confirm the recipient, subject, "
        "and content with the user before sending if there is any ambiguity."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "Recipient email address. If the user names a person, pick "
                    "their address from the contact list above. If omitted, a "
                    "configured default recipient is used (if one is set)."
                ),
            },
            "subject": {
                "type": "string",
                "description": "Subject line of the email.",
            },
            "body": {
                "type": "string",
                "description": "Plain-text body content of the email.",
            },
        },
        "required": ["subject", "body"],
    }

    def spec(self) -> Dict[str, Any]:
        """Include the contact list in the description so the model can choose."""
        base = super().spec()
        base["description"] = self.description + _contacts_description()
        return base

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        subject = (kwargs.get("subject") or "").strip()
        body = kwargs.get("body") or ""
        recipient = (kwargs.get("to") or os.environ.get("EMAIL_DEFAULT_TO") or "").strip()

        host = os.environ.get("SMTP_HOST", "").strip()
        username = os.environ.get("SMTP_USERNAME", "").strip() or None
        password = os.environ.get("SMTP_PASSWORD", "") or None
        sender = (os.environ.get("EMAIL_FROM") or username or "").strip()
        use_ssl = _truthy(os.environ.get("SMTP_USE_SSL"))
        default_port = 465 if use_ssl else 587
        try:
            port = int(os.environ.get("SMTP_PORT", str(default_port)))
        except ValueError:
            return {"error": "SMTP_PORT must be an integer."}

        # Validate configuration and arguments before attempting to send.
        if not host:
            return {"error": "Email is not configured: SMTP_HOST is not set."}
        if not sender:
            return {"error": "Email is not configured: set EMAIL_FROM or SMTP_USERNAME."}
        if not recipient:
            return {"error": "No recipient: provide `to` or set EMAIL_DEFAULT_TO."}
        if "@" not in recipient:
            return {
                "error": (
                    f"'{recipient}' is not an email address. Pick the contact's "
                    "address from the contact list and pass that as `to`."
                )
            }
        if not subject:
            return {"error": "Subject is required."}

        logger.info(
            "Tool call: send_email to=%s subject=%r (%d chars body)",
            recipient,
            subject,
            len(body),
        )

        try:
            await asyncio.to_thread(
                _send_email_blocking,
                host=host,
                port=port,
                username=username,
                password=password,
                use_ssl=use_ssl,
                sender=sender,
                recipient=recipient,
                subject=subject,
                body=body,
            )
        except smtplib.SMTPAuthenticationError:
            logger.exception("send_email authentication failed")
            return {"error": "SMTP authentication failed. Check credentials."}
        except (smtplib.SMTPException, OSError) as exc:
            logger.exception("send_email failed")
            return {"error": f"Failed to send email: {exc}"}

        return {"status": "sent", "to": recipient, "subject": subject}

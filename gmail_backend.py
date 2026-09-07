"""Direct Gmail API transport using the user's authorised gog OAuth client.

The refresh token remains in gog's keyring. Only a short-lived access token is
cached in an owner-only local file. Tokens never enter argv or diagnostic output.
"""
import base64
import email.message
import email.policy
import email.utils
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from local_store import MailError


class ProviderError(MailError):
    def __init__(self, message, *, status=None, retry_after=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.uncertain = uncertain


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(value):
    if not value: return None
    try:
        return max(0,float(value))
    except ValueError:
        try: return max(0,email.utils.parsedate_to_datetime(value).timestamp()-time.time())
        except (ValueError,TypeError,OverflowError): return None


def remaining(deadline):
    duration = deadline - time.monotonic()
    if duration <= 0:
        raise ProviderError("Deadline elapsed before the next provider request")
    return duration


def http_json(url, *, data=None, headers=None, method=None, timeout=20):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # Do not echo request headers, tokens, or arbitrary provider response bodies.
        retry = e.headers.get("Retry-After")
        raise ProviderError(f"Google API returned HTTP {e.code}", status=e.code,
                            retry_after=retry_delay(retry),
                            uncertain=e.code >= 500 or e.code == 408) from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ProviderError("Google API connection/response failed", uncertain=True) from e


def secure_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Gmail:
    def __init__(self, account, state):
        self.account = account.casefold()
        self.state = Path(state)
        key = hashlib.sha256(self.account.encode()).hexdigest()[:24]
        self.cache = self.state / "auth" / (key + ".json")

    def token(self, *, force=False, timeout=20):
        deadline = time.monotonic() + timeout
        self.cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(self.cache.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.05, remaining(deadline)))
            if self.cache.exists() and not force:
                cached = json.loads(self.cache.read_text())
                if cached.get("account") == self.account and cached.get("expires_at", 0) > time.time() + 60:
                    return cached["access_token"]
            from configuration import credentials_path
            credential_file = credentials_path()
            if not credential_file.exists():
                raise ProviderError("Gmail is not connected for amail; run amail connect")
            credentials = json.loads(credential_file.read_text())
            with tempfile.TemporaryDirectory(prefix="oauth-", dir=self.cache.parent) as directory:
                export = Path(directory) / "refresh.json"
                p = subprocess.run(["gog", "auth", "tokens", "export", self.account,
                                    "--client", "amail", "--out", str(export), "--no-input"],
                                   capture_output=True, text=True, timeout=min(10,remaining(deadline)))
                if p.returncode or not export.exists():
                    raise ProviderError("Gmail authorisation is unavailable; run amail connect")
                grant = json.loads(export.read_text())
                if grant.get("email", "").casefold() != self.account:
                    raise ProviderError("Gmail authorisation belongs to a different account")
                form = urllib.parse.urlencode({"client_id": credentials["client_id"],
                       "client_secret": credentials["client_secret"],
                       "refresh_token": grant["refresh_token"], "grant_type": "refresh_token"}).encode()
                try:
                    tokens = http_json("https://oauth2.googleapis.com/token", data=form,
                                       headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=remaining(deadline))
                except ProviderError as e:
                    raise ProviderError("Gmail token refresh failed; run amail connect", status=e.status) from e
            token = tokens.get("access_token")
            if not token:
                raise ProviderError("Gmail authorisation returned no access token")
            profile = http_json("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                                headers={"Authorization": "Bearer " + token}, timeout=remaining(deadline))
            if profile.get("emailAddress", "").casefold() != self.account:
                raise ProviderError("Gmail API identity does not match the requested account")
            secure_json(self.cache, {"account": self.account, "access_token": token,
                                     "expires_at": time.time() + float(tokens.get("expires_in", 3600))})
            return token

    def request(self, path, *, payload=None, method=None, query=None, timeout=20):
        deadline = time.monotonic() + timeout
        if not path.startswith("/") or ".." in path:
            raise MailError("Invalid Gmail API resource path")
        url = "https://gmail.googleapis.com/gmail/v1/users/me" + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(2):
            token = self.token(force=attempt == 1, timeout=remaining(deadline))
            try:
                return http_json(url, data=data, method=method, timeout=remaining(deadline),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            except ProviderError as e:
                # HTTP 401 is an explicit rejection, so a fresh-token retry cannot
                # duplicate an accepted message. Never retry ambiguous send errors.
                if e.status != 401 or attempt:
                    raise

    def profile(self):
        return self.request("/profile")

    def find(self, query, maximum=100, timeout=20):
        return self.request("/messages", query={"q": query, "maxResults": maximum,
                                                "includeSpamTrash": "true"}, timeout=timeout).get("messages", [])

    def raw(self, message_id):
        identifier = urllib.parse.quote(message_id, safe="")
        result = self.request("/messages/" + identifier, query={"format": "raw"})
        encoded = result.get("raw", "")
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)), result


def build_mime(request):
    message = email.message.EmailMessage(policy=email.policy.SMTP)
    message["From"] = request["formatted_sender"]
    for key in ("to", "cc", "bcc"):
        if request[key]:
            message[key.capitalize()] = ", ".join(request[key])
    message["Subject"] = request["subject"]
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = request.get("message_id") or email.utils.make_msgid(domain=request["sender"].split("@", 1)[1])
    if request.get("request_id"):
        message["X-Amail-Request-ID"] = request["request_id"]
    if request.get("in_reply_to"):
        message["In-Reply-To"] = request["in_reply_to"]
        message["References"] = request.get("references") or request["in_reply_to"]
    message.set_content(request["body"], charset="utf-8", cte="base64")
    for value in request["attach"]:
        path = Path(value)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        main, sub = kind.split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=main, subtype=sub,
                               filename=path.name, cte="base64")
    return message.as_bytes()


def submit(request, timeout, state):
    deadline=time.monotonic()+timeout
    client = Gmail(request.get("auth_account") or request["sender"], state)
    if client.account != request["sender"].casefold():
        aliases = client.request("/settings/sendAs", timeout=remaining(deadline)).get("sendAs", [])
        if not any(a.get("sendAsEmail", "").casefold() == request["sender"].casefold() and a.get("verificationStatus") == "accepted" for a in aliases):
            raise ProviderError("Requested Gmail sender is not a verified send-as alias")
    if request.get("in_reply_to") and not request.get("gmail_thread_id"):
        matches = client.find("rfc822msgid:" + request["in_reply_to"].strip("<>"), maximum=10,timeout=remaining(deadline))
        if len(matches) > 1:
            raise ProviderError("Original Gmail thread lookup is ambiguous")
        if matches:
            request["gmail_thread_id"] = matches[0].get("threadId")
    payload = {"raw": base64.urlsafe_b64encode(build_mime(request)).decode()}
    if request.get("gmail_thread_id"):
        payload["threadId"] = request["gmail_thread_id"]
    result = client.request("/messages/send", payload=payload, timeout=remaining(deadline))
    message_id = result.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ProviderError("Gmail submission returned no message ID; reconcile its outcome", uncertain=True)
    accepted = {"state": "accepted", "provider": "gmail", "provider_message_id": message_id,
            "provider_thread_id": result.get("threadId"), "requested_message_id": request.get("message_id"),
            "ref": "gmail:" + request["account_id"] + ":" + message_id,
            "evidence": {"type": "gmail_messages_send_response", "message_id": message_id},
            "verification_source": "Gmail API accepted the message and returned its server ID"}
    try:
        metadata = client.request("/messages/" + urllib.parse.quote(message_id, safe=""),
                                  query={"format":"metadata", "metadataHeaders":["Message-ID","From"]}, timeout=remaining(deadline))
        headers = {h["name"].casefold():h["value"] for h in metadata.get("payload",{}).get("headers",[])}
        accepted.update(message_id=headers.get("message-id"), effective_from=headers.get("from"))
    except ProviderError:
        accepted["metadata_pending"] = True
    return accepted

"""Conservative v1 credential signals, never a guarantee of detecting arbitrary secrets."""
import re


_SIGNALS = re.compile(
    r"(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|secret|private[_ -]?key|authorization|密码|口令|密钥|令牌)"
    r"[\s\"']*(?:[:=：]|是|为)"
    r"|\bbearer\s+\S+"
    r"|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{16})\b"
    r"|[a-z][a-z0-9+.-]{0,31}://[^\s/@:]+:[^\s/@]+@",
    re.IGNORECASE,
)


def credential_signal(text: str) -> bool:
    return _SIGNALS.search(text) is not None


def _prefixes(value: str) -> str:
    return "(?:" + "|".join(re.escape(value[:end]) for end in range(1, len(value) + 1)) + ")"


# A possible unfinished signal must stay with the caller until disambiguated. No raw tail
# is persisted: durable stream receipts bind its HMAC, so losing it requires stopping the stream.
_KEYWORDS = ("password", "passwd", "pwd", "secret", "authorization", "密码", "口令", "密钥", "令牌")
_PENDING = re.compile("|".join([
    *(_prefixes(word) + r"$" for word in _KEYWORDS),
    r"(?:password|passwd|pwd|secret|authorization|密码|口令|密钥|令牌)[\s\"']*$",
    *(_prefixes(first) + r"$" for first in ("api", "access", "private")),
    r"(?:api|access|private)[_ -]?$",
    *(r"(?:" + first + r")[_ -]?" + _prefixes(last) + r"$"
      for first, last in (("api", "key"), ("access", "token"), ("private", "key"))),
    r"(?:api[_ -]?key|access[_ -]?token|private[_ -]?key)[\s\"']*$",
    r"\b" + _prefixes("bearer") + r"$", r"\bbearer\s*$",
    r"\b(?:s|sk|sk-[A-Za-z0-9_-]{0,11}|g|gh|gh[pousr]|gh[pousr]_[A-Za-z0-9]{0,11}|A|AK|AKI|AKIA[A-Z0-9]{0,15})$",
    _prefixes("-----BEGIN ") + r"$",
    r"-----BEGIN [A-Z]*$", r"-----BEGIN [A-Z]+ $", r"-----BEGIN (?:[A-Z]+ )?" + _prefixes("PRIVATE KEY-----") + r"$",
    r"[a-z][a-z0-9+.-]{0,31}(?::/?|://[^\s/@:]*(?::[^\s/@]*)?)?$",
]), re.IGNORECASE)


def stream_safe_prefix(text: str, *, complete: bool = False) -> int:
    """Return releasable code points for the existing finite credential detector only."""
    if credential_signal(text):
        return 0
    if complete:
        return len(text)
    pending = _PENDING.search(text)
    return pending.start() if pending else len(text)

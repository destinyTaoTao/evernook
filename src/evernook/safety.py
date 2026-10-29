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

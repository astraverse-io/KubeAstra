"""Shared redaction helpers for tool output (ConfigMaps, Helm values/manifests).

Structured redaction, used by both the ConfigMap tools and the Helm tools so the
logic stays in one place (a separate copy would drift and risk reintroducing the
PEM-body leak that was fixed on the ConfigMap side):

- a sensitive *key name* redacts the whole value
- PEM private-key blocks (BEGIN..body..END, including unterminated ones) are
  redacted whole — never just the BEGIN line
- otherwise only individual secret-assignment lines are redacted, so a multi-line
  config (e.g. a Jenkins plugins.txt) stays readable
- rendered ``kind: Secret`` manifest documents have their ``data:`` /
  ``stringData:`` maps redacted
"""
import re

REDACTED = "***redacted***"
PEM_REDACTION = "***redacted (private key)***"

# A *key name* that looks sensitive => redact its whole value aggressively
# (e.g. a key literally called "db_password"). "credential" is included here.
SENSITIVE_KEY_RE = re.compile(
    r"pass(word|wd)?|secret|token|api[-_]?key|private[-_]?key|credential|bearer|"
    r"client[-_]?secret|access[-_]?key",
    re.IGNORECASE,
)

# A single *value line* that looks like a secret assignment => redact just that
# line. "credential" is deliberately excluded here so names like
# "plain-credentials:1.8" in a Jenkins plugins.txt are not mistaken for secrets.
_SECRET_LINE_RE = re.compile(
    r"^(?P<prefix>\s*[\w.\-/]*"
    r"(?:pass(?:word|wd)?|secret|token|api[-_]?key|private[-_]?key|"
    r"client[-_]?secret|access[-_]?key|bearer))\s*[:=]\s*\S",
    re.IGNORECASE,
)

# A complete PEM private-key block (BEGIN line, base64 body, END line).
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)
# Defensive: a BEGIN block with no matching END (e.g. truncated value).
_PEM_OPEN_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*\Z",
    re.DOTALL | re.IGNORECASE,
)

_KIND_SECRET_RE = re.compile(r"^\s*kind:\s*[\"']?Secret[\"']?\s*$")
_DATA_BLOCK_RE = re.compile(r"^(\s*)(data|stringData):\s*$")
# Inline flow-map form, e.g. `data: {password: c2VjcmV0, user: YWRtaW4=}`.
_INLINE_DATA_RE = re.compile(r"^(\s*(?:data|stringData):\s*)\{(.*)\}\s*$")
_YAML_KV_RE = re.compile(r"^(\s*[\w.\-]+:\s*)\S.*$")


def truncate(text, cap: int) -> str:
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + f"… [truncated, {len(text)} bytes total]"


def redact_line(line: str) -> str:
    """Redact one line only if it looks like a secret assignment."""
    m = _SECRET_LINE_RE.match(line)
    if m:
        return f"{m.group('prefix').rstrip()}: {REDACTED}"
    return line


def _redact_pem(value: str) -> str:
    value = _PEM_BLOCK_RE.sub(PEM_REDACTION, value)
    return _PEM_OPEN_RE.sub(PEM_REDACTION, value)


# Inline secret assignment anywhere in a line (for free-text / prose like Helm
# NOTES, where a `key: value` anchor at line start is too strict).
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(pass(?:word|wd)?|secret|token|api[-_]?key|private[-_]?key|"
    r"client[-_]?secret|access[-_]?key|bearer)\b\s*[:=]\s*\S+"
)


def redact_prose(text: str, cap: int) -> str:
    """Redact inline secret assignments anywhere in free text (e.g. Helm NOTES).

    Unlike redact_value (anchored at a line-start key), this catches
    `password: x` / `token=y` embedded mid-line in prose. PEM blocks redacted too.
    """
    text = _redact_pem(text or "")
    redacted = _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}: {REDACTED}", text)
    return truncate(redacted, cap)


def redact_value(key: str, value: str, cap: int) -> str:
    """Structured redaction for a single key's value.

    Sensitive key name -> whole value redacted. PEM blocks redacted whole.
    Otherwise only secret-assignment lines are redacted.
    """
    value = value or ""
    if SENSITIVE_KEY_RE.search(key or ""):
        return REDACTED
    value = _redact_pem(value)
    redacted = (
        "\n".join(redact_line(ln) for ln in value.splitlines())
        if "\n" in value else redact_line(value)
    )
    return truncate(redacted, cap)


def redact_manifest(text: str, cap: int) -> str:
    """Redact secrets in a rendered (possibly multi-document) manifest.

    - Within any ``kind: Secret`` document, redact the values under
      ``data:`` / ``stringData:`` (including multi-line base64 blocks).
    - PEM private-key blocks are redacted whole.
    - Line-level secret-assignment redaction is applied elsewhere.
    """
    text = _redact_pem(text or "")
    out = []
    in_secret_doc = False
    in_data_block = False
    data_indent = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "---":  # document boundary
            in_secret_doc = False
            in_data_block = False
            out.append(raw)
            continue
        if _KIND_SECRET_RE.match(raw):
            in_secret_doc = True
            out.append(raw)
            continue
        if in_secret_doc:
            inline = _INLINE_DATA_RE.match(raw)
            if inline:
                # Inline flow map: redact the whole map content (keys not preserved).
                inner = inline.group(2).strip()
                out.append(inline.group(1) + ("{}" if not inner else "{" + REDACTED + "}"))
                in_data_block = False
                continue
            m = _DATA_BLOCK_RE.match(raw)
            if m:
                in_data_block = True
                data_indent = len(m.group(1))
                out.append(raw)
                continue
            if in_data_block:
                indent = len(raw) - len(raw.lstrip())
                if not stripped:
                    # Blank line inside the data block — keep the block open.
                    out.append(raw)
                    continue
                if indent > data_indent:
                    # Everything under a Secret's data: is sensitive. Redact the
                    # value of a key:value line; redact a continuation/block-scalar
                    # body line (e.g. multi-line base64) entirely.
                    kv = _YAML_KV_RE.match(raw)
                    out.append(kv.group(1) + REDACTED if kv else " " * indent + REDACTED)
                    continue
                # dedented non-blank line -> data block ended
                in_data_block = False
        out.append(redact_line(raw))
    return truncate("\n".join(out), cap)

"""Which names mark a credential.

One rule for both places that need it: the log export, which leaves out a
line pairing such a name with a value, and the pipeline check that flags code
passing such a value to a log. Standard library only, so the check can import
it without the server's dependencies.
"""

import re

_WORD_BREAK_RE = re.compile(r"[_.\-]|(?<=[a-z0-9])(?=[A-Z])")

#: How a credential's name ends once its words are run together in lower case:
#: ``user_auth_token`` and ``accessToken`` both end in ``token``.
CREDENTIAL_NAME_ENDINGS = (
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "secretkey",
    "privatekey",
    "credential",
    "credentials",
    "authorization",
    "cookie",
)

# Credentials only as a URL's query parameter; in prose they are ordinary words.
_QUERY_CREDENTIAL_NAMES = frozenset(("key", "sig", "signature", "auth"))


def is_credential_name(name: str, in_query: bool = False) -> bool:
    """Whether ``name`` — an identifier, header, key or query parameter —
    names a credential. ``in_query`` widens it to the short names a URL's
    query uses for one."""
    words = [w.lower() for w in _WORD_BREAK_RE.split(name) if w]
    if not words:
        return False
    if "".join(words).endswith(CREDENTIAL_NAME_ENDINGS):
        return True
    return in_query and words[-1] in _QUERY_CREDENTIAL_NAMES

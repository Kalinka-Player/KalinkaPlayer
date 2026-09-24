"""How to sign in to a share, apart from the client that does it.

Kept out of :mod:`.smb` so what a configuration asks for can be worked out,
and compared, on an installation without the SMB client library.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SmbCredentials:
    """The login one music source reads its share with.

    Empty credentials mean the guest access most NAS boxes offer for a media
    share. That is still a logon, under the name
    :data:`~.smb.GUEST_USERNAME`, not an anonymous one — ``spnego`` cannot
    build a context without a username at all, and a server configured for
    guests maps an unknown name onto its guest account.

    @note Hashable, so one storage and its connections serve every source
        signing in the same way.
    """

    username: str = ""
    password: str = field(default="", repr=False)
    encrypt: bool = False

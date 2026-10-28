"""Pure rules about the bearer token — shared by both services.

The token is the one secret that guards a shell. Two rules live here,
and they live here so the agent and the proxy cannot disagree about
them:

1. **A placeholder is not a secret.** The operator template ships
   ``CHANGE_ME_openssl_rand_hex_32`` as a value to replace. That string
   is public — it is in the repository, in every clone of it — so a
   deployment that still carries it has no secret at all, only the
   appearance of one. The stack refuses to serve with it.

2. **An empty token is not a secret either.** The agent and the proxy
   already refuse to serve when their token is empty (fail-closed);
   this module is where that rule is written down once.

The check is a *prefix* check, deliberately: the template's placeholders
all begin with ``CHANGE_ME``, and an operator who writes a real token
that happens to contain the word is not a case worth a false refusal.
"""

from __future__ import annotations

#: The prefix every placeholder in ``.env.example`` carries.
PLACEHOLDER_PREFIX = "CHANGE_ME"


def is_placeholder(token: str) -> bool:
    """True when ``token`` is still the template's placeholder.

    An empty token is **not** a placeholder: it is a missing secret,
    which the services refuse on their own (fail-closed). Keeping the
    two apart means the operator gets two different messages for two
    different mistakes.
    """
    return token.startswith(PLACEHOLDER_PREFIX)


def token_problem(token: str) -> str | None:
    """Return why ``token`` cannot be used, or ``None`` when it is fine.

    The token is not only a bearer token. The compose file interpolates
    it into a JSON document — ``"key":"${OPENAPI_PROXY_TOKEN}"`` inside
    ``TOOL_SERVER_CONNECTIONS`` — and that document is parsed by Open
    WebUI. A quote or a backslash in the token turns it into something
    Open WebUI cannot parse, and the failure is quiet: the UI comes up
    without the sandbox tool and nobody is told why.

    The same token also travels in an ``Authorization`` header, where a
    newline is a header injection rather than a broken JSON.

    So the token has to be non-empty, made of printable ASCII without
    whitespace, and not a placeholder. ``openssl rand -hex 32``
    satisfies all of that, which is why the template asks for it.
    """
    if not token:
        return "the token is empty"
    if is_placeholder(token):
        return (
            f"the token is still the template placeholder ({token!r}) — it is a "
            f"published string, not a secret. Generate a real one with: "
            f"openssl rand -hex 32"
        )
    bad = sorted(
        {
            char
            for char in token
            if char.isspace() or not (0x20 <= ord(char) < 0x7F) or char in '"\\'
        }
    )
    if bad:
        shown = ", ".join(repr(char) for char in bad)
        return (
            f"the token contains {shown} — it is embedded into the Open WebUI "
            f"tool connection as JSON and travels in an HTTP header, and those "
            f"characters break one or the other. Regenerate it with: "
            f"openssl rand -hex 32"
        )
    return None

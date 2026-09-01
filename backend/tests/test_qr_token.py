"""The signed, versioned site QR token (Requirement 8.4, 8.6, 8.7).

These are the claims that make the token an *attendance control* rather than a decorated site number:
a forged or tampered token is rejected, a token minted before a regeneration is rejected once the
site's version moves past it, and a separate-mode token carries its action inside the signature so it
cannot be flipped from check-out to check-in. They run against the pure token module with no database
and no HTTP; the endpoint wiring is pinned in `test_qr_api.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.core import qr_token
from app.core.qr_token import InvalidToken, QrAction


def test_a_minted_token_round_trips_to_its_site_and_version():
    """Requirement 8.4: a token verifies back to the site and version it was minted for."""
    site_id = uuid.uuid4()
    token = qr_token.mint(site_id, 1)

    parsed = qr_token.verify(token, expected_version=1)

    assert parsed.site_id == site_id
    assert parsed.version == 1
    assert parsed.action is None


def test_the_token_is_opaque_and_does_not_expose_the_site_number():
    """Requirement 8.4: the payload is signed and base64-encoded, not a readable identifier.

    The site number never enters the token — only the id does, sealed — so knowing "S-42" tells an
    attacker nothing about how to build a token for it.
    """
    token = qr_token.mint(uuid.uuid4(), 1)
    assert token.startswith("site:")
    assert "S-42" not in token


def test_a_forged_token_is_rejected():
    """Requirement 8.7: a token this key did not sign does not verify.

    Tampering with a single character of the signature is enough; the payload is unchanged but the
    HMAC no longer matches it.
    """
    token = qr_token.mint(uuid.uuid4(), 1)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(InvalidToken):
        qr_token.parse(forged)


def test_a_token_with_a_swapped_site_is_rejected():
    """A payload edited to name a different site breaks the signature (Requirement 8.4)."""
    real = qr_token.mint(uuid.uuid4(), 1)
    other = qr_token.mint(uuid.uuid4(), 1)
    # Graft the payload of one token onto the signature of another.
    real_payload = real[len("site:") :].split(".")[0]
    other_signature = other[len("site:") :].split(".")[1]
    frankenstein = f"site:{real_payload}.{other_signature}"

    with pytest.raises(InvalidToken):
        qr_token.parse(frankenstein)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-token",
        "site:",
        "site:onlyonepart",
        "site:@@@.@@@",
        "wrongprefix:abc.def",
    ],
)
def test_malformed_tokens_are_rejected(bad: str):
    """Requirement 8.7: a malformed token is refused, not parsed leniently."""
    with pytest.raises(InvalidToken):
        qr_token.parse(bad)


def test_a_token_from_a_previous_version_is_revoked():
    """Requirement 8.6, 8.7: after a regeneration bumps the version, the old token is rejected.

    The old token is well-formed and correctly signed — it simply names a version the site has moved
    past, so `verify` against the current version refuses it exactly as it would a forgery.
    """
    site_id = uuid.uuid4()
    old_token = qr_token.mint(site_id, 1)

    # The site regenerates; its current version is now 2.
    with pytest.raises(InvalidToken):
        qr_token.verify(old_token, expected_version=2)

    # A token minted at the new version is accepted.
    new_token = qr_token.mint(site_id, 2)
    assert qr_token.verify(new_token, expected_version=2).version == 2


def test_a_separate_mode_token_carries_its_action():
    """Requirement 8.3: the two separate-mode codes differ only in their signed action."""
    site_id = uuid.uuid4()
    check_in = qr_token.mint(site_id, 1, action=QrAction.CHECK_IN)
    check_out = qr_token.mint(site_id, 1, action=QrAction.CHECK_OUT)

    assert qr_token.verify(check_in, expected_version=1).action is QrAction.CHECK_IN
    assert qr_token.verify(check_out, expected_version=1).action is QrAction.CHECK_OUT
    assert check_in != check_out


def test_the_action_cannot_be_flipped_without_breaking_the_signature():
    """A check-out token edited to read check-in fails verification (Requirement 8.3, 8.4)."""
    site_id = uuid.uuid4()
    check_out = qr_token.mint(site_id, 1, action=QrAction.CHECK_OUT)
    # Splice the payload half of a check-in token onto the check-out signature.
    check_in_payload = qr_token.mint(site_id, 1, action=QrAction.CHECK_IN)
    payload = check_in_payload[len("site:") :].split(".")[0]
    signature = check_out[len("site:") :].split(".")[1]

    with pytest.raises(InvalidToken):
        qr_token.parse(f"site:{payload}.{signature}")


def test_minting_rejects_a_non_positive_version():
    with pytest.raises(ValueError, match="version"):
        qr_token.mint(uuid.uuid4(), 0)

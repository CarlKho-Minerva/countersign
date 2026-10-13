from decimal import Decimal

import pytest

from countersign.grant import Grant, GrantError, check_scope, issue, new_keypair, verify

NOW = 1_800_000_000


def _grant(**over):
    base = dict(
        grant_id="g1",
        caller="agent:travel-desk",
        form_id="conf-reg",
        origins=("form.test",),
        facts=("full_name", "email"),
        max_total=Decimal("0.00"),
        may_submit=False,
        expires_at=NOW + 600,
    )
    base.update(over)
    return Grant(**base)


def test_round_trip():
    priv, pub = new_keypair()
    token = issue(priv, _grant())
    assert verify(pub, token, now=NOW) == _grant()


def test_tampered_payload_is_refused():
    priv, pub = new_keypair()
    token = issue(priv, _grant())
    other = issue(priv, _grant(facts=("full_name", "email", "passport_no")))
    forged = other.split(".")[0] + "." + token.split(".")[1]
    with pytest.raises(GrantError, match="signature"):
        verify(pub, forged, now=NOW)


def test_wrong_issuer_is_refused():
    priv, _ = new_keypair()
    _, other_pub = new_keypair()
    with pytest.raises(GrantError, match="signature"):
        verify(other_pub, issue(priv, _grant()), now=NOW)


def test_expired_is_refused():
    priv, pub = new_keypair()
    token = issue(priv, _grant(expires_at=NOW - 1))
    with pytest.raises(GrantError, match="expired"):
        verify(pub, token, now=NOW)


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "!!.??", "e30.e30"])
def test_malformed_is_refused(token):
    _, pub = new_keypair()
    with pytest.raises(GrantError):
        verify(pub, token, now=NOW)


def test_scope_allows_what_was_granted():
    assert check_scope(_grant(), form_id="conf-reg", origin="form.test", facts=["email"]) == []


def test_scope_refuses_other_form_origin_and_facts():
    refusals = check_scope(
        _grant(), form_id="other-form", origin="evil.test", facts=["email", "passport_no"]
    )
    kinds = {r.split(":")[0] for r in refusals}
    assert kinds == {"form", "origin", "fact"}
    assert any("passport_no" in r for r in refusals)


def test_scope_refuses_submit_without_permission():
    assert check_scope(
        _grant(), form_id="conf-reg", origin="form.test", facts=[], submit=True
    ) == ["submit: grant g1 does not allow submitting"]


def test_total_over_limit():
    g = _grant(max_total=Decimal("0.00"))
    assert g.total_ok(Decimal("0.00"))
    assert not g.total_ok(Decimal("1278.00"))
    assert _grant(max_total=None).total_ok(Decimal("9999"))


def test_consent_rule_round_trips_and_defaults_to_ask():
    priv, pub = new_keypair()
    assert verify(pub, issue(priv, _grant()), now=NOW).consent == "ask"
    assert verify(pub, issue(priv, _grant(consent="required-only")), now=NOW).consent == "required-only"
    with pytest.raises(GrantError, match="unknown consent policy"):
        verify(pub, issue(priv, _grant(consent="tick-everything")), now=NOW)

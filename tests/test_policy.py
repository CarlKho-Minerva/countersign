import pytest

from countersign.policy import EgressPolicy


def policy(**over):
    base = dict(allow_hosts=frozenset({"form.test"}), allow_ports=frozenset({80, 443}))
    base.update(over)
    return EgressPolicy(**base)


def test_allows_the_form_origin():
    assert policy().check_host("form.test", 443) is None
    assert policy().check_host("FORM.test.", 80) is None


@pytest.mark.parametrize(
    "host",
    ["collector.test", "evil-form.test", "form.test.evil.com", "sub.form.test", "", "form.test@evil.com"],
)
def test_denies_other_hosts(host):
    assert policy().check_host(host, 443) is not None


def test_denies_other_ports():
    assert "port" in policy().check_host("form.test", 22)


@pytest.mark.parametrize("host", ["169.254.169.254", "127.0.0.1", "10.0.0.5", "[::1]", "2130706433", "0x7f.1"])
def test_denies_ip_literals_even_if_listed(host):
    assert policy(allow_hosts=frozenset({host})).check_host(host, 80) is not None


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "172.17.0.2", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0", "100.64.0.1"])
def test_denies_names_that_resolve_inside(addr):
    # A public name that points at the host, the metadata service or a neighbour.
    assert "private" in policy().check_resolved("form.test", addr)


def test_allows_public_addresses():
    assert policy().check_resolved("form.test", "93.184.216.34") is None


def test_private_fixture_hosts_must_be_named_explicitly():
    p = policy(private_ok=frozenset({"form.test"}))
    assert p.check_resolved("form.test", "172.18.0.3") is None
    assert p.check_resolved("form.test", "169.254.169.254") is not None  # never the metadata range


def test_env_round_trip():
    p = policy(private_ok=frozenset({"form.test"}), allow_ports=frozenset({8080}))
    assert EgressPolicy.from_env(p.to_env()) == p

from countersign.fingerprint import drift, fingerprint


def field(key, **over):
    base = dict(key=key, tag="input", type="text", label=key.title(), required=False, options=[], value="", checked=False, visible=True)
    base.update(over)
    return base


FORM = [
    field("full_name", required=True),
    field("email", type="email", required=True),
    field("country", tag="select", type="select", options=["US", "PH", "JP"]),
    field("csrf", type="hidden", value="a1b2", visible=False),
]


def test_same_shape_same_fingerprint_whatever_the_values():
    live = [dict(f) for f in FORM]
    live[0]["value"] = "Someone Else"
    live[3]["value"] = "zz99"  # tokens rotate on every load
    live[2]["options"] = ["JP", "PH", "US"]  # order is presentation
    assert fingerprint(live) == fingerprint(FORM)
    assert not drift(FORM, live)


def test_added_removed_and_changed_fields_are_named():
    live = [dict(f) for f in FORM if f["key"] != "email"]
    live.append(field("share_with_sponsors", type="checkbox"))
    live[1] = field("country", tag="select", type="select", options=["US", "PH"], required=True)
    d = drift(FORM, live)
    assert d
    assert d.added == ["share_with_sponsors"]
    assert d.removed == ["email"]
    assert d.changed == {"country": ["required False -> True", "options removed: JP"]}
    assert fingerprint(live) != fingerprint(FORM)


def test_label_change_is_not_drift_but_type_change_is():
    relabelled = [dict(f) for f in FORM]
    relabelled[0]["label"] = "Your full legal name"
    assert not drift(FORM, relabelled)
    retyped = [dict(f) for f in FORM]
    retyped[0]["type"] = "password"
    assert drift(FORM, retyped).changed == {"full_name": ["type text -> password"]}


def test_duplicate_keys_do_not_hide_a_change():
    twice = FORM + [field("full_name", type="password")]
    assert drift(FORM, twice)

import pytest

from countersign.consent import classify, decide


def box(label, key="c1", required=False):
    return {"label": label, "key": key, "required": required, "type": "checkbox"}


@pytest.mark.parametrize("label", [
    "I agree to the terms and conditions",
    "I accept the Privacy Policy",
    "I have read and agree to the Code of Conduct",
    "I acknowledge the refund policy",
    "By ticking this box you consent to our\nevent rules",
])
def test_terms_are_accepted_required_or_not(label):
    assert classify(label) == "terms"
    assert decide(box(label)) is True and decide(box(label, required=True)) is True


@pytest.mark.parametrize("label,key", [
    ("Send me the newsletter", "c1"),
    ("Share my address with event sponsors", "c1"),
    ("Yes, I'd like special offers and promotions", "c1"),
    ("Our partners may contact me", "c1"),
    ("I agree to the terms and to receive marketing emails", "c1"),  # bundled: marketing wins
    ("Yes please", "marketing_opt_in"),  # the name says what the label hides
    ("Keep me updated", "c1"),
])
def test_marketing_is_declined_even_when_the_form_demands_it(label, key):
    assert classify(label, key) == "marketing"
    assert decide(box(label, key)) is False and decide(box(label, key, required=True)) is False


@pytest.mark.parametrize("label", [
    "I certify that I am legally authorized to work in the United States",
    "I am over 18",
    "I confirm the information above is accurate",
    "I am a U.S. citizen or permanent resident",
])
def test_a_required_statement_of_fact_is_left_to_the_owner(label):
    assert classify(label) == "other"
    assert decide(box(label, required=True)) is None


@pytest.mark.parametrize("label", ["Remember my details", "Add event insurance", "I am over 18", ""])
def test_anything_optional_is_declined(label):
    assert decide(box(label)) is False

import pytest

from chaarteeke.telephony.numbers import to_e164_in


@pytest.mark.parametrize(
    "raw,want",
    [
        ("+919876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("+91 98765-43210", "+919876543210"),
        ("00919876543210", "+919876543210"),
        ("", None),
        (None, None),
        ("anonymous", None),
        ("5551234", None),
        ("+14155550123", None),
        ("+911912345678", None),
        ("1234567890", None),
    ],
)
def test_normalise(raw, want):
    assert to_e164_in(raw) == want

from pathlib import Path


def test_booking_payment_messages_source_is_valid_utf8() -> None:
    source_path = Path('src/dance_studio/core/booking_payment_messages.py')
    data = source_path.read_bytes()
    decoded = data.decode('utf-8')

    assert '\\u0410\\u0431\\u043e\\u043d\\u0435\\u043c\\u0435\\u043d\\u0442' in decoded
    assert '\\u0410\\u0440\\u0435\\u043d\\u0434\\u0430' in decoded
    assert '\\u0418\\u043d\\u0434\\u0438\\u0432\\u0438\\u0434\\u0443\\u0430\\u043b\\u044c\\u043d\\u043e\\u0435' in decoded

    # Typical mojibake fragments we saw in production logs/snapshots.
    assert 'Ãðóïïà' not in decoded
    assert 'Àáîíåìåíò' not in decoded
    assert 'Âðåìÿ' not in decoded

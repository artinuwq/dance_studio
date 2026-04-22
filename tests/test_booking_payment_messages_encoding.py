from pathlib import Path


def test_booking_payment_messages_source_is_valid_utf8() -> None:
    source_path = Path('src/dance_studio/core/booking_payment_messages.py')
    data = source_path.read_bytes()
    decoded = data.decode('utf-8')

    assert 'Абонемент:' in decoded
    assert 'Аренда:' in decoded
    assert 'Индивидуальное занятие:' in decoded

    # Typical mojibake fragments we saw in production logs/snapshots.
    assert 'Ãðóïïà' not in decoded
    assert 'Àáîíåìåíò' not in decoded
    assert 'Âðåìÿ' not in decoded

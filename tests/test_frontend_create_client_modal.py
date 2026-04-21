from pathlib import Path


def test_create_client_modal_orders_primary_fields_before_abonements():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    name_pos = source.index('id="client-create-name"')
    phone_pos = source.index('id="client-create-phone"')
    staff_note_pos = source.index('id="client-create-staff-notes"')
    abonement_section_pos = source.index('id="client-create-abonement-list"')

    assert name_pos < phone_pos < staff_note_pos < abonement_section_pos
    assert 'id="client-create-abonement-open-btn"' in source
    assert 'id="client-create-abonement-editor"' in source


def test_create_client_modal_submits_initial_abonements_payload():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "let clientCreateInitialAbonements = [];" in source
    assert "function openClientCreateAbonementEditor(editIndexRaw = null) {" in source
    assert "function saveClientCreateAbonementDraft() {" in source
    assert "payload.initial_abonements = clientCreateInitialAbonements.map((item) => ({" in source
    assert "Можно добавить максимум 3 абонемента" in source

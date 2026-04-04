from pathlib import Path


def test_client_detail_modal_supports_manual_merge_and_delete_actions():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "function buildClientVkLink(client)" in source
    assert "const hasTelegramContext = Boolean(" in source
    assert "|| getTelegramInitDataFromUrl()" in source
    assert "function prefillMergeClientSelectors(client)" in source
    assert "function handleClientMergeSearchInput(value)" in source
    assert "function selectClientMergeCandidate(userId)" in source
    assert "function clearClientMergeSide(side)" in source
    assert "function renderClientMergeUi(rawQuery = null)" in source
    assert "function openCurrentClientMerge()" in source
    assert "function openCurrentClientVk()" in source
    assert "function syncClientDetailSocialButtons(client)" in source
    assert "async function deleteCurrentClient()" in source
    assert "openMergeClientsModal(selectedClientForActions.id)" in source
    assert "/api/admin/clients/${client.id}/archive" in source
    assert "Имя, телефон, @username, tg id, #id" in source
    assert "Параметры объединения" in source
    assert "Связь" in source
    assert "Работа с клиентом" in source
    assert "Управление" in source
    assert 'class="client-detail-social-row"' in source
    assert 'id="client-detail-vk-btn"' in source
    assert 'id="client-detail-merge-btn"' in source
    assert 'id="client-detail-delete-btn"' in source

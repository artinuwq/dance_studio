from pathlib import Path


def test_vk_phone_request_statuses_are_human_readable():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "function getVkPhoneRequestErrorMessage(errorCode)" in source
    assert "Эта функция работает только внутри VK Mini App" in source
    assert "Запрашиваем номер телефона у VK..." in source
    assert "Не удалось получить номер телефона. Попробуйте еще раз." in source
    assert "Номер сохранен. Уведомления VK можно включить позже внутри VK Mini App." in source
    assert "Не удалось запросить номер телефона. Попробуйте еще раз." in source

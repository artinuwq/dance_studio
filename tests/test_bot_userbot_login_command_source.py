from pathlib import Path


def test_bot_exposes_userbot_login_command_flow():
    source = Path("src/dance_studio/bot/bot.py").read_text(encoding="utf-8")

    assert 'Command("userbot_login")' in source
    assert "UserbotLoginStates" in source
    assert "Отправьте номер телефона для входа в user-bot." in source
    assert "Пришлите код из Telegram одним сообщением." in source
    assert "Для этого аккаунта включен пароль 2FA." in source
    assert "Эту команду запускайте только в личке с ботом." in source

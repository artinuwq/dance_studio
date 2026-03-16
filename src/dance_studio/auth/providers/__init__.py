from .passkey import PasskeyAuthProvider
from .phone import PhoneCodeAuthProvider
from .telegram import TelegramAuthProvider
from .vk import VkMiniAppAuthProvider

__all__ = [
    'TelegramAuthProvider',
    'VkMiniAppAuthProvider',
    'PhoneCodeAuthProvider',
    'PasskeyAuthProvider',
]

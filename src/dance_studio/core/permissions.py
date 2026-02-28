"""
Система управления разрешениями и правами доступа
"""

# Определение ролей и их права
ROLES = {
    "тех. админ": {
        "name": "Технический администратор",
        "permissions": [
            # Все права системы
            "cancel_lesson",
            "rent_hall",
            "view_personal_lessons",
            "create_news",
            "manage_schedule",
            "create_direction",
            "create_group",
            "assign_lesson",
            "verify_certificate",
            "view_all_users",
            "manage_staff",
            "manage_permissions",
            "manage_mailings",
            "view_stats",
            "system_settings",
            "manage_backups",
            "full_system_access",  # Полный доступ к системе
        ]
    },
    "учитель": {
        "name": "Учитель",
        "permissions": [
            "cancel_lesson",           # Отменить/перенести занятие
            "rent_hall",               # Аренда зала (с другой ценой)
            "view_personal_lessons",   # Просмотр своих индивидуальных занятий
        ]
    },
    "администратор": {
        "name": "Администратор",
        "permissions": [
            "create_news",             # Создание новостей
            "manage_schedule",         # Курирование расписания
            "create_direction",        # Создание направления
            "create_group",            # Создание групп
            "assign_lesson",           # Назначение стабильных занятий
            "verify_certificate",      # Проверка справки/продление абонемента
            "view_all_users",          # Просмотр всех пользователей
            "manage_mailings",         # Управление рассылками
            "view_stats",              # Просмотр статистики/отчётов
        ]
    },
    "старший админ": {
        "name": "Старший админ",
        "permissions": [
            # Все права администратора
            "create_news",
            "manage_schedule",
            "create_direction",
            "create_group",
            "assign_lesson",
            "verify_certificate",
            "view_all_users",
            # Дополнительные права, как у владельца
            "manage_staff",
            "manage_permissions",
            "system_settings",
            "manage_backups",
            "manage_mailings",
            "view_stats",
        ]
    },
    "владелец": {
        "name": "Владелец",
        "permissions": [
            # Все права администратора
            "create_news",
            "manage_schedule",
            "create_direction",
            "create_group",
            "assign_lesson",
            "verify_certificate",
            "view_all_users",
            # Дополнительные права владельца
            "manage_staff",            # Управление должностями/персоналом
            "manage_permissions",      # Управление разрешениями
            "system_settings",         # Системные настройки
            "manage_backups",         # ???????????????????? ????????????????
            "manage_mailings",         # Управление рассылками
            "view_stats",              # Просмотр статистики/отчётов
        ]
    }
}


def has_permission(staff_position: str, permission: str) -> bool:
    """
    Проверяет, есть ли у пользователя определенное разрешение
    
    Args:
        staff_position: Должность сотрудника ('учитель', 'администратор', 'старший админ', 'владелец')
        permission: Требуемое разрешение
    
    Returns:
        bool: True если есть разрешение, False иначе
    """
    if staff_position not in ROLES:
        return False
    
    return permission in ROLES[staff_position]["permissions"]


def can_manage_schedule(staff_position: str) -> bool:
    """Может ли курировать расписание"""
    return has_permission(staff_position, "manage_schedule")


def can_manage_staff(staff_position: str) -> bool:
    """Может ли управлять персоналом"""
    return has_permission(staff_position, "manage_staff")


def can_create_news(staff_position: str) -> bool:
    """Может ли создавать новости"""
    return has_permission(staff_position, "create_news")


def can_rent_hall(staff_position: str) -> bool:
    """Может ли арендовать зал"""
    return has_permission(staff_position, "rent_hall")


def get_role_name(staff_position: str) -> str:
    """Получить человекочитаемое имя роли"""
    if staff_position in ROLES:
        return ROLES[staff_position]["name"]
    return "Неизвестная роль"


def get_all_permissions(staff_position: str) -> list:
    """Получить список всех разрешений для роли"""
    if staff_position in ROLES:
        return ROLES[staff_position]["permissions"]
    return []

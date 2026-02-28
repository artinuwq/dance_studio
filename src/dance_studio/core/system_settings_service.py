import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dance_studio.db.models import AppSetting, AppSettingChange


class SettingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: str
    default: Any
    description: str = ""
    is_public: bool = False
    min_value: float | None = None
    max_value: float | None = None
    max_length: int | None = None
    allowed_values: tuple[Any, ...] | None = None


SETTING_SPECS: dict[str, SettingSpec] = {
    "contacts.admin_username": SettingSpec(
        key="contacts.admin_username",
        value_type="string",
        default="@admin_username",
        description="Telegram username of admin account for user contact.",
        is_public=True,
        max_length=33,
    ),
    "contacts.bot_username": SettingSpec(
        key="contacts.bot_username",
        value_type="string",
        default="@bot_username",
        description="Telegram username of the studio bot account.",
        is_public=True,
        max_length=33,
    ),
    "rental.base_hour_price_rub": SettingSpec(
        key="rental.base_hour_price_rub",
        value_type="int",
        default=2500,
        description="Base hall rental price per hour in RUB.",
        is_public=True,
        min_value=0,
        max_value=1_000_000,
    ),
    "rental.min_duration_minutes": SettingSpec(
        key="rental.min_duration_minutes",
        value_type="int",
        default=60,
        description="Minimum allowed rental duration in minutes.",
        is_public=True,
        min_value=30,
        max_value=480,
    ),
    "rental.step_minutes": SettingSpec(
        key="rental.step_minutes",
        value_type="int",
        default=30,
        description="Rental selection step in minutes.",
        is_public=True,
        min_value=5,
        max_value=120,
    ),
    "rental.open_hour_local": SettingSpec(
        key="rental.open_hour_local",
        value_type="int",
        default=8,
        description="Hall opening hour (local time, 0-23).",
        is_public=True,
        min_value=0,
        max_value=23,
    ),
    "rental.close_hour_local": SettingSpec(
        key="rental.close_hour_local",
        value_type="int",
        default=22,
        description="Hall closing hour (local time, 1-24).",
        is_public=True,
        min_value=1,
        max_value=24,
    ),
    "rental.require_admin_approval": SettingSpec(
        key="rental.require_admin_approval",
        value_type="bool",
        default=True,
        description="If true, rentals should be approved by admin workflow.",
        is_public=False,
    ),
    "abonements.single_visit_price_rub": SettingSpec(
        key="abonements.single_visit_price_rub",
        value_type="int",
        default=1200,
        description="Single-visit abonement price in RUB.",
        is_public=False,
        min_value=0,
        max_value=1_000_000,
    ),
    "abonements.trial_price_rub": SettingSpec(
        key="abonements.trial_price_rub",
        value_type="int",
        default=0,
        description="Trial abonement price in RUB.",
        is_public=False,
        min_value=0,
        max_value=1_000_000,
    ),
    "abonements.multi_single_prices_json": SettingSpec(
        key="abonements.multi_single_prices_json",
        value_type="json",
        default={},
        description="Multi abonement prices for single group by direction_type and lessons bucket (4/8/12).",
        is_public=False,
    ),
    "abonements.multi_bundle_prices_json": SettingSpec(
        key="abonements.multi_bundle_prices_json",
        value_type="json",
        default={
            "dance": {
                "2": {"4": 6400, "8": 12800, "12": 19200},
                "3": {"4": 8400, "8": 16800, "12": 25200},
            },
            "sport": {
                "2": {"4": 6400, "8": 12800, "12": 19200},
                "3": {"4": 8400, "8": 16800, "12": 25200},
            },
        },
        description="Multi abonement prices for 2/3-group bundles by direction_type and lessons bucket (4/8/12).",
        is_public=False,
    ),
}

USERNAME_SETTING_KEYS = {
    "contacts.admin_username",
    "contacts.bot_username",
}
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _from_json(value_json: str) -> Any:
    return json.loads(value_json)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    raise SettingValidationError("bool value expected")


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        raise SettingValidationError("int value expected")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingValidationError("int value expected") from exc
    return parsed


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise SettingValidationError("float value expected")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingValidationError("float value expected") from exc
    return parsed


def _coerce_string(value: Any) -> str:
    if value is None:
        raise SettingValidationError("string value expected")
    return str(value).strip()


def _normalize_telegram_username(value: str) -> str:
    raw = value.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if not TELEGRAM_USERNAME_RE.fullmatch(raw):
        raise SettingValidationError("telegram username must be like @username (5-32 chars, latin, digits, _)")
    return f"@{raw.lower()}"


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SettingValidationError("invalid json payload") from exc
    return value


def _enforce_numeric_bounds(spec: SettingSpec, value: int | float) -> None:
    if spec.min_value is not None and value < spec.min_value:
        raise SettingValidationError(f"value must be >= {spec.min_value}")
    if spec.max_value is not None and value > spec.max_value:
        raise SettingValidationError(f"value must be <= {spec.max_value}")


def _validate_with_spec(spec: SettingSpec, raw_value: Any) -> Any:
    if spec.value_type == "bool":
        normalized = _coerce_bool(raw_value)
    elif spec.value_type == "int":
        normalized = _coerce_int(raw_value)
        _enforce_numeric_bounds(spec, normalized)
    elif spec.value_type == "float":
        normalized = _coerce_float(raw_value)
        _enforce_numeric_bounds(spec, normalized)
    elif spec.value_type == "string":
        normalized = _coerce_string(raw_value)
        if spec.max_length is not None and len(normalized) > spec.max_length:
            raise SettingValidationError(f"string too long (max {spec.max_length})")
        if spec.key in USERNAME_SETTING_KEYS:
            normalized = _normalize_telegram_username(normalized)
    elif spec.value_type == "json":
        normalized = _coerce_json(raw_value)
    else:
        raise SettingValidationError(f"unsupported setting type: {spec.value_type}")

    if spec.allowed_values is not None and normalized not in spec.allowed_values:
        raise SettingValidationError(f"value must be one of: {', '.join(map(str, spec.allowed_values))}")

    return normalized


def _spec_or_raise(key: str) -> SettingSpec:
    spec = SETTING_SPECS.get(key)
    if not spec:
        raise KeyError(f"Unknown setting key: {key}")
    return spec


def serialize_setting(row: AppSetting) -> dict[str, Any]:
    value = _from_json(row.value_json)
    return {
        "key": row.key,
        "value": value,
        "value_type": row.value_type,
        "description": row.description or "",
        "is_public": bool(row.is_public),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by_staff_id": row.updated_by_staff_id,
    }


def serialize_setting_change(row: AppSettingChange) -> dict[str, Any]:
    return {
        "id": row.id,
        "setting_id": row.setting_id,
        "setting_key": row.setting_key,
        "old_value": _from_json(row.old_value_json) if row.old_value_json else None,
        "new_value": _from_json(row.new_value_json),
        "changed_by_staff_id": row.changed_by_staff_id,
        "change_reason": row.change_reason or "",
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def serialize_setting_spec(spec: SettingSpec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "value_type": spec.value_type,
        "default": spec.default,
        "description": spec.description,
        "is_public": spec.is_public,
        "min_value": spec.min_value,
        "max_value": spec.max_value,
        "max_length": spec.max_length,
        "allowed_values": list(spec.allowed_values) if spec.allowed_values is not None else None,
    }


def list_setting_specs(public_only: bool = False) -> list[dict[str, Any]]:
    specs = sorted(SETTING_SPECS.values(), key=lambda item: item.key)
    if public_only:
        specs = [spec for spec in specs if spec.is_public]
    return [serialize_setting_spec(spec) for spec in specs]


def ensure_default_settings(db) -> dict[str, AppSetting]:
    existing_rows = db.query(AppSetting).all()
    by_key = {row.key: row for row in existing_rows}
    changed = False

    for key, spec in SETTING_SPECS.items():
        default_value = _validate_with_spec(spec, spec.default)
        if key not in by_key:
            row = AppSetting(
                key=key,
                value_json=_to_json(default_value),
                value_type=spec.value_type,
                description=spec.description,
                is_public=spec.is_public,
            )
            db.add(row)
            by_key[key] = row
            changed = True
            continue

        row = by_key[key]
        if row.value_type != spec.value_type:
            row.value_type = spec.value_type
            changed = True
        if (row.description or "") != spec.description:
            row.description = spec.description
            changed = True
        if bool(row.is_public) != bool(spec.is_public):
            row.is_public = spec.is_public
            changed = True

        # Keep stored value if valid; reset to default if invalid.
        try:
            current_value = _from_json(row.value_json)
            normalized = _validate_with_spec(spec, current_value)
            normalized_json = _to_json(normalized)
            if row.value_json != normalized_json:
                row.value_json = normalized_json
                changed = True
        except Exception:
            row.value_json = _to_json(default_value)
            changed = True

    if changed:
        db.flush()

    return by_key


def get_setting_value(db, key: str) -> Any:
    spec = _spec_or_raise(key)
    row = db.query(AppSetting).filter_by(key=key).first()
    if not row:
        rows = ensure_default_settings(db)
        row = rows.get(key)
    if not row:
        return spec.default
    value = _from_json(row.value_json)
    return _validate_with_spec(spec, value)


def list_settings(db, public_only: bool = False) -> list[dict[str, Any]]:
    ensure_default_settings(db)
    query = db.query(AppSetting)
    if public_only:
        query = query.filter(AppSetting.is_public.is_(True))
    rows = query.order_by(AppSetting.key.asc()).all()
    return [serialize_setting(row) for row in rows]


def list_setting_changes(db, key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = min(max(int(limit or 1), 1), 500)
    query = db.query(AppSettingChange)
    if key:
        query = query.filter(AppSettingChange.setting_key == key)
    rows = query.order_by(AppSettingChange.created_at.desc(), AppSettingChange.id.desc()).limit(safe_limit).all()
    return [serialize_setting_change(row) for row in rows]


def update_setting(
    db,
    *,
    key: str,
    raw_value: Any,
    changed_by_staff_id: int | None = None,
    reason: str | None = None,
    source: str = "api",
) -> dict[str, Any]:
    spec = _spec_or_raise(key)
    ensure_default_settings(db)

    row = db.query(AppSetting).filter_by(key=key).first()
    if not row:
        row = AppSetting(
            key=key,
            value_json=_to_json(_validate_with_spec(spec, spec.default)),
            value_type=spec.value_type,
            description=spec.description,
            is_public=spec.is_public,
        )
        db.add(row)
        db.flush()

    normalized_value = _validate_with_spec(spec, raw_value)
    new_value_json = _to_json(normalized_value)
    old_value_json = row.value_json
    if old_value_json == new_value_json:
        row.updated_by_staff_id = changed_by_staff_id
        row.updated_at = datetime.now()
        db.flush()
        return serialize_setting(row)

    row.value_json = new_value_json
    row.value_type = spec.value_type
    row.description = spec.description
    row.is_public = spec.is_public
    row.updated_by_staff_id = changed_by_staff_id
    row.updated_at = datetime.now()

    change = AppSettingChange(
        setting_id=row.id,
        setting_key=row.key,
        old_value_json=old_value_json,
        new_value_json=new_value_json,
        changed_by_staff_id=changed_by_staff_id,
        change_reason=(reason or "").strip() or None,
        source=(source or "api").strip()[:32] or "api",
    )
    db.add(change)
    db.flush()
    return serialize_setting(row)

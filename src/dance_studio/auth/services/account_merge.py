from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, or_

from dance_studio.auth.services.common import (
    ensure_user_phone,
    normalize_phone_e164,
    phone_operation_lock,
    set_primary_phone,
)
from dance_studio.db.models import (
    Attendance,
    AttendanceIntention,
    AttendanceReminder,
    AuthIdentity,
    BookingRequest,
    GroupAbonement,
    Notification,
    NotificationChannel,
    NotificationPreference,
    PasskeyCredential,
    PaymentTransaction,
    SessionRecord,
    User,
    UserPhone,
    UserMergeEvent,
    WebPushSubscription,
)


class AccountMergeService:
    @staticmethod
    def _sql_phone_digits(column):
        return func.replace(
            func.replace(
                func.replace(
                    func.replace(
                        func.replace(func.coalesce(column, ""), " ", ""),
                        "-",
                        "",
                    ),
                    "(",
                    "",
                ),
                ")",
                "",
            ),
            "+",
            "",
        )

    def score_user(self, db, user_id: int) -> int:
        return (
            db.query(BookingRequest).filter(BookingRequest.user_id == user_id).count() * 5
            + db.query(Attendance).filter(Attendance.user_id == user_id).count() * 3
            + db.query(GroupAbonement).filter(GroupAbonement.user_id == user_id).count() * 4
            + db.query(PaymentTransaction).filter(PaymentTransaction.user_id == user_id).count() * 4
        )

    def choose_primary_user(self, db, user_a_id: int, user_b_id: int) -> tuple[int, int]:
        user_a = db.query(User).filter(User.id == user_a_id).first()
        user_b = db.query(User).filter(User.id == user_b_id).first()
        if user_a and user_b:
            if user_a.telegram_id and not user_b.telegram_id:
                return user_a_id, user_b_id
            if user_b.telegram_id and not user_a.telegram_id:
                return user_b_id, user_a_id

        score_a = self.score_user(db, user_a_id)
        score_b = self.score_user(db, user_b_id)
        if score_a == score_b:
            if user_a and user_b and user_a.registered_at and user_b.registered_at:
                if user_a.registered_at <= user_b.registered_at:
                    return user_a_id, user_b_id
                return user_b_id, user_a_id
            return user_a_id, user_b_id
        if score_a >= score_b:
            return user_a_id, user_b_id
        return user_b_id, user_a_id

    def _needs_manual_merge(self, db, user_id: int) -> bool:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.requires_manual_merge:
            return True
        critical_rows = (
            db.query(PaymentTransaction.id).filter(PaymentTransaction.user_id == user_id).first(),
            db.query(GroupAbonement.id).filter(GroupAbonement.user_id == user_id).first(),
        )
        return any(row is not None for row in critical_rows)

    def _find_legacy_phone_match_user_ids(self, db, *, phone_e164: str, exclude_user_id: int) -> list[int]:
        digits = "".join(ch for ch in phone_e164 if ch.isdigit())
        if len(digits) < 10:
            return []
        phone_tail = digits[-10:]
        phone_expr = self._sql_phone_digits(User.phone)
        primary_phone_expr = self._sql_phone_digits(User.primary_phone)
        candidates = (
            db.query(User)
            .filter(User.is_archived.is_(False), User.id != exclude_user_id)
            .filter(
                or_(
                    phone_expr.like(f"%{phone_tail}"),
                    primary_phone_expr.like(f"%{phone_tail}"),
                )
            )
            .order_by(User.id.asc())
            .all()
        )
        matched_ids: list[int] = []
        for candidate in candidates:
            values = [candidate.primary_phone, candidate.phone]
            if any(normalize_phone_e164(value) == phone_e164 for value in values if value):
                matched_ids.append(candidate.id)
        return sorted(set(matched_ids))

    def _upsert_legacy_phone_row(self, db, *, user: User | None, source: str) -> None:
        if not user:
            return
        normalized = normalize_phone_e164(user.primary_phone or user.phone)
        if not normalized:
            return

        existing = (
            db.query(UserPhone)
            .filter(UserPhone.user_id == user.id, UserPhone.phone_e164 == normalized)
            .order_by(UserPhone.id.asc())
            .first()
        )
        if existing:
            if user.phone_verified_at and existing.verified_at is None:
                existing.verified_at = user.phone_verified_at
            if existing.is_primary or user.primary_phone or user.phone:
                set_primary_phone(db, user_id=user.id, phone_row=existing)
            return

        ensure_user_phone(
            db,
            user_id=user.id,
            phone_e164=normalized,
            source=source,
            verified_at=user.phone_verified_at,
            is_primary=True,
        )

    def _merge_legacy_user_fields(self, *, primary: User | None, secondary: User | None) -> None:
        if not primary or not secondary:
            return

        if not primary.username and secondary.username:
            primary.username = secondary.username
        if not primary.email and secondary.email:
            primary.email = secondary.email
        if not primary.birth_date and secondary.birth_date:
            primary.birth_date = secondary.birth_date
        if not primary.photo_path and secondary.photo_path:
            primary.photo_path = secondary.photo_path
        if not primary.preferred_notification_channel and secondary.preferred_notification_channel:
            primary.preferred_notification_channel = secondary.preferred_notification_channel
        if not primary.telegram_id and secondary.telegram_id:
            primary.telegram_id = secondary.telegram_id

    def _reassign_dependencies(self, db, *, source_user_id: int, target_user_id: int) -> None:
        db.query(BookingRequest).filter(BookingRequest.user_id == source_user_id).update({BookingRequest.user_id: target_user_id}, synchronize_session=False)
        db.query(Attendance).filter(Attendance.user_id == source_user_id).update({Attendance.user_id: target_user_id}, synchronize_session=False)
        db.query(AttendanceIntention).filter(AttendanceIntention.user_id == source_user_id).update({AttendanceIntention.user_id: target_user_id}, synchronize_session=False)
        db.query(AttendanceReminder).filter(AttendanceReminder.user_id == source_user_id).update({AttendanceReminder.user_id: target_user_id}, synchronize_session=False)
        db.query(GroupAbonement).filter(GroupAbonement.user_id == source_user_id).update({GroupAbonement.user_id: target_user_id}, synchronize_session=False)
        db.query(PaymentTransaction).filter(PaymentTransaction.user_id == source_user_id).update({PaymentTransaction.user_id: target_user_id}, synchronize_session=False)
        db.query(Notification).filter(Notification.user_id == source_user_id).update({Notification.user_id: target_user_id}, synchronize_session=False)
        db.query(NotificationChannel).filter(NotificationChannel.user_id == source_user_id).update({NotificationChannel.user_id: target_user_id}, synchronize_session=False)
        db.query(NotificationPreference).filter(NotificationPreference.user_id == source_user_id).update({NotificationPreference.user_id: target_user_id}, synchronize_session=False)
        db.query(WebPushSubscription).filter(WebPushSubscription.user_id == source_user_id).update({WebPushSubscription.user_id: target_user_id}, synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.user_id == source_user_id).update({SessionRecord.user_id: target_user_id}, synchronize_session=False)

    def _merge_identities(self, db, *, source_user_id: int, target_user_id: int) -> None:
        source_identities = db.query(AuthIdentity).filter(AuthIdentity.user_id == source_user_id).order_by(AuthIdentity.id.asc()).all()
        for identity in source_identities:
            existing = (
                db.query(AuthIdentity)
                .filter(
                    AuthIdentity.user_id == target_user_id,
                    AuthIdentity.provider == identity.provider,
                    AuthIdentity.provider_user_id == identity.provider_user_id,
                )
                .first()
            )
            if existing:
                existing.last_login_at = max(existing.last_login_at or datetime.min, identity.last_login_at or datetime.min)
                existing.is_verified = existing.is_verified or identity.is_verified
                existing.provider_payload_json = existing.provider_payload_json or identity.provider_payload_json
                db.delete(identity)
            else:
                identity.user_id = target_user_id

    def _merge_phones(self, db, *, source_user_id: int, target_user_id: int) -> None:
        source_phones = db.query(UserPhone).filter(UserPhone.user_id == source_user_id).order_by(UserPhone.id.asc()).all()
        target_phones = db.query(UserPhone).filter(UserPhone.user_id == target_user_id).order_by(UserPhone.id.asc()).all()
        by_phone = {row.phone_e164: row for row in target_phones}
        chosen_primary = next((row for row in target_phones if row.is_primary and row.verified_at is not None), None)

        for phone in source_phones:
            existing = by_phone.get(phone.phone_e164)
            if existing:
                existing.verified_at = existing.verified_at or phone.verified_at
                existing.source = existing.source or phone.source
                existing.is_primary = existing.is_primary or phone.is_primary
                db.delete(phone)
                if existing.verified_at and existing.is_primary:
                    chosen_primary = existing
            else:
                phone.user_id = target_user_id
                by_phone[phone.phone_e164] = phone
                if phone.verified_at and phone.is_primary:
                    chosen_primary = phone

        final_phones = db.query(UserPhone).filter(UserPhone.user_id == target_user_id).order_by(UserPhone.id.asc()).all()
        if not chosen_primary:
            chosen_primary = next((row for row in final_phones if row.verified_at is not None), None) or next(iter(final_phones), None)
        if chosen_primary:
            set_primary_phone(db, user_id=target_user_id, phone_row=chosen_primary)

    def _merge_passkeys(self, db, *, source_user_id: int, target_user_id: int) -> None:
        source_credentials = db.query(PasskeyCredential).filter(PasskeyCredential.user_id == source_user_id).order_by(PasskeyCredential.id.asc()).all()
        for credential in source_credentials:
            existing = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential.credential_id).first()
            if existing and existing.user_id == target_user_id:
                existing.sign_count = max(existing.sign_count or 0, credential.sign_count or 0)
                existing.last_used_at = max(existing.last_used_at or datetime.min, credential.last_used_at or datetime.min)
                db.delete(credential)
            else:
                credential.user_id = target_user_id

    def merge_users(self, db, *, user_a_id: int, user_b_id: int, reason: str, strategy: str = "score_based") -> tuple[int, int]:
        primary_id, secondary_id = self.choose_primary_user(db, user_a_id, user_b_id)
        if primary_id == secondary_id:
            return primary_id, secondary_id

        primary = db.query(User).filter(User.id == primary_id).first()
        secondary = db.query(User).filter(User.id == secondary_id).first()
        self._upsert_legacy_phone_row(db, user=primary, source="legacy_primary")
        self._upsert_legacy_phone_row(db, user=secondary, source="legacy_secondary")

        self._merge_identities(db, source_user_id=secondary_id, target_user_id=primary_id)
        self._merge_phones(db, source_user_id=secondary_id, target_user_id=primary_id)
        self._merge_passkeys(db, source_user_id=secondary_id, target_user_id=primary_id)
        self._reassign_dependencies(db, source_user_id=secondary_id, target_user_id=primary_id)

        secondary = db.query(User).filter(User.id == secondary_id).first()
        primary = db.query(User).filter(User.id == primary_id).first()
        self._merge_legacy_user_fields(primary=primary, secondary=secondary)
        if secondary:
            secondary.is_archived = True
            secondary.status = "inactive"
            secondary.merged_to_user_id = primary_id
        if primary and secondary and secondary.last_login_at and (not primary.last_login_at or secondary.last_login_at > primary.last_login_at):
            primary.last_login_at = secondary.last_login_at

        db.add(
            UserMergeEvent(
                source_user_id=secondary_id,
                target_user_id=primary_id,
                merge_reason=reason,
                merge_strategy=strategy,
                case_status="resolved",
                conflict_source=reason,
                review_result="approved" if reason.startswith("manual_") else None,
                resolved_at=datetime.utcnow(),
                payload_json=json.dumps({"merged_at": datetime.utcnow().isoformat()}, ensure_ascii=False),
            )
        )

        return primary_id, secondary_id

    def try_merge_by_phone(self, db, *, user_id: int, phone: str, source: str = "phone_verification") -> dict:
        normalized_phone = normalize_phone_e164(phone)
        if not normalized_phone:
            return {"status": "no_phone"}

        with phone_operation_lock(db, normalized_phone):
            source_user = db.query(User).filter(User.id == user_id, User.is_archived.is_(False)).first()
            if not source_user:
                return {"status": "no_user"}

            all_phone_rows = (
                db.query(UserPhone)
                .filter(UserPhone.phone_e164 == normalized_phone)
                .order_by(UserPhone.id.asc())
                .all()
            )
            source_phone = next((row for row in all_phone_rows if row.user_id == user_id), None)
            verified_matches = [row for row in all_phone_rows if row.verified_at is not None and row.user_id != user_id]
            match_user_ids = sorted({row.user_id for row in verified_matches})
            if not match_user_ids:
                match_user_ids = self._find_legacy_phone_match_user_ids(
                    db,
                    phone_e164=normalized_phone,
                    exclude_user_id=user_id,
                )

            if not match_user_ids:
                if not source_phone:
                    source_phone = UserPhone(
                        user_id=user_id,
                        phone_e164=normalized_phone,
                        source=source,
                        verified_at=datetime.utcnow(),
                        is_primary=False,
                    )
                    db.add(source_phone)
                    db.flush()
                else:
                    source_phone.verified_at = source_phone.verified_at or datetime.utcnow()
                    source_phone.source = source or source_phone.source
                set_primary_phone(db, user_id=user_id, phone_row=source_phone)
                return {"status": "no_match"}

            if len(match_user_ids) > 1:
                db.add(
                    UserMergeEvent(
                        source_user_id=user_id,
                        target_user_id=match_user_ids[0],
                        merge_reason="phone_conflict",
                        merge_strategy="auto_by_phone",
                        case_status="pending_review",
                        conflict_source=source,
                        payload_json=json.dumps(
                            {
                                "phone": normalized_phone,
                                "conflict_user_ids": match_user_ids,
                                "source": source,
                                "conflict_at": datetime.utcnow().isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                return {"status": "conflict", "conflict_user_ids": match_user_ids}

            other = db.query(User).filter(User.id == match_user_ids[0], User.is_archived.is_(False)).first()
            if not other:
                return {"status": "no_match"}
            if source_user.requires_manual_merge or other.requires_manual_merge or self._needs_manual_merge(db, user_id) or self._needs_manual_merge(db, other.id):
                if source_phone:
                    source_phone.verified_at = source_phone.verified_at or datetime.utcnow()
                    set_primary_phone(db, user_id=user_id, phone_row=source_phone)
                source_user.requires_manual_merge = True
                other.requires_manual_merge = True
                db.add(
                    UserMergeEvent(
                        source_user_id=user_id,
                        target_user_id=other.id,
                        merge_reason="manual_merge_required",
                        merge_strategy="auto_by_phone_blocked",
                        case_status="pending_review",
                        conflict_source=source,
                        payload_json=json.dumps({"phone": normalized_phone, "source": source}, ensure_ascii=False),
                    )
                )
                return {"status": "manual_review_required", "conflict_user_ids": [user_id, other.id]}

            if not source_phone:
                source_phone = UserPhone(
                    user_id=user_id,
                    phone_e164=normalized_phone,
                    source=source,
                    verified_at=datetime.utcnow(),
                    is_primary=False,
                )
                db.add(source_phone)
                db.flush()
            else:
                source_phone.verified_at = source_phone.verified_at or datetime.utcnow()
            set_primary_phone(db, user_id=user_id, phone_row=source_phone)

            primary_id, secondary_id = self.merge_users(
                db,
                user_a_id=user_id,
                user_b_id=other.id,
                reason="phone_match",
                strategy="auto_by_phone",
            )
            return {
                "status": "merged",
                "primary_user_id": primary_id,
                "secondary_user_id": secondary_id,
            }

    def list_pending_merge_cases(self, db) -> list[UserMergeEvent]:
        return (
            db.query(UserMergeEvent)
            .filter(UserMergeEvent.case_status == "pending_review")
            .order_by(UserMergeEvent.created_at.asc(), UserMergeEvent.id.asc())
            .all()
        )

    def get_merge_case(self, db, *, event_id: int) -> UserMergeEvent | None:
        return db.query(UserMergeEvent).filter(UserMergeEvent.id == event_id).first()

    def review_merge_case(
        self,
        db,
        *,
        event_id: int,
        decision: str,
        reviewed_by: int,
        reason: str | None = None,
    ) -> UserMergeEvent | None:
        event = self.get_merge_case(db, event_id=event_id)
        if not event:
            return None

        now = datetime.utcnow()
        event.reviewed_by = reviewed_by
        event.reviewed_at = now

        if decision == "approve":
            primary_id, secondary_id = self.merge_users(
                db,
                user_a_id=event.source_user_id,
                user_b_id=event.target_user_id,
                reason="manual_review_approved",
                strategy="manual_review",
            )
            event.case_status = "resolved"
            event.review_result = "approved"
            event.resolved_at = now
            event.payload_json = json.dumps(
                {
                    "decision_reason": reason,
                    "primary_user_id": primary_id,
                    "secondary_user_id": secondary_id,
                    "reviewed_at": now.isoformat(),
                },
                ensure_ascii=False,
            )
            return event

        if decision == "reject":
            event.case_status = "resolved"
            event.review_result = "rejected"
            event.resolved_at = now
            event.payload_json = json.dumps(
                {"decision_reason": reason, "reviewed_at": now.isoformat()},
                ensure_ascii=False,
            )
            return event

        if decision == "ignore":
            event.case_status = "ignored"
            event.review_result = "ignored"
            event.resolved_at = now
            event.payload_json = json.dumps(
                {"decision_reason": reason, "reviewed_at": now.isoformat()},
                ensure_ascii=False,
            )
            return event

        raise ValueError("unsupported_merge_review_decision")

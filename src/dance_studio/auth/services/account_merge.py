from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import or_

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
    UserMergeEvent,
    WebPushSubscription,
)


class AccountMergeService:
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

    def merge_users(self, db, *, user_a_id: int, user_b_id: int, reason: str, strategy: str = "score_based") -> tuple[int, int]:
        primary_id, secondary_id = self.choose_primary_user(db, user_a_id, user_b_id)
        if primary_id == secondary_id:
            return primary_id, secondary_id

        db.query(BookingRequest).filter(BookingRequest.user_id == secondary_id).update({BookingRequest.user_id: primary_id}, synchronize_session=False)
        db.query(Attendance).filter(Attendance.user_id == secondary_id).update({Attendance.user_id: primary_id}, synchronize_session=False)
        db.query(AttendanceIntention).filter(AttendanceIntention.user_id == secondary_id).update({AttendanceIntention.user_id: primary_id}, synchronize_session=False)
        db.query(AttendanceReminder).filter(AttendanceReminder.user_id == secondary_id).update({AttendanceReminder.user_id: primary_id}, synchronize_session=False)
        db.query(GroupAbonement).filter(GroupAbonement.user_id == secondary_id).update({GroupAbonement.user_id: primary_id}, synchronize_session=False)
        db.query(PaymentTransaction).filter(PaymentTransaction.user_id == secondary_id).update({PaymentTransaction.user_id: primary_id}, synchronize_session=False)
        db.query(AuthIdentity).filter(AuthIdentity.user_id == secondary_id).update({AuthIdentity.user_id: primary_id}, synchronize_session=False)
        db.query(PasskeyCredential).filter(PasskeyCredential.user_id == secondary_id).update({PasskeyCredential.user_id: primary_id}, synchronize_session=False)
        db.query(Notification).filter(Notification.user_id == secondary_id).update({Notification.user_id: primary_id}, synchronize_session=False)
        db.query(NotificationChannel).filter(NotificationChannel.user_id == secondary_id).update({NotificationChannel.user_id: primary_id}, synchronize_session=False)
        db.query(NotificationPreference).filter(NotificationPreference.user_id == secondary_id).update({NotificationPreference.user_id: primary_id}, synchronize_session=False)
        db.query(WebPushSubscription).filter(WebPushSubscription.user_id == secondary_id).update({WebPushSubscription.user_id: primary_id}, synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.user_id == secondary_id).update({SessionRecord.user_id: primary_id}, synchronize_session=False)

        primary = db.query(User).filter(User.id == primary_id).first()
        secondary = db.query(User).filter(User.id == secondary_id).first()
        if primary and secondary:
            if not primary.telegram_id and secondary.telegram_id:
                primary.telegram_id = secondary.telegram_id

        if secondary:
            secondary.is_archived = True
            secondary.status = "inactive"
            secondary.merged_to_user_id = primary_id

        db.add(
            UserMergeEvent(
                source_user_id=secondary_id,
                target_user_id=primary_id,
                merge_reason=reason,
                merge_strategy=strategy,
                payload_json=json.dumps({"merged_at": datetime.utcnow().isoformat()}, ensure_ascii=False),
            )
        )

        return primary_id, secondary_id

    def try_merge_by_phone(self, db, *, user_id: int, phone: str, source: str = "phone_verification") -> dict:
        normalized_phone = (phone or "").strip()
        if not normalized_phone:
            return {"status": "no_phone"}

        matches = (
            db.query(User)
            .filter(
                or_(User.primary_phone == normalized_phone, User.phone == normalized_phone),
                User.phone_verified_at.isnot(None),
                User.id != user_id,
                User.is_archived.is_(False),
            )
            .all()
        )

        if not matches:
            return {"status": "no_match"}

        if len(matches) > 1:
            conflict_ids = [u.id for u in matches]
            db.add(
                UserMergeEvent(
                    source_user_id=user_id,
                    target_user_id=conflict_ids[0],
                    merge_reason="phone_conflict",
                    merge_strategy="auto_by_phone",
                    payload_json=json.dumps(
                        {
                            "phone": normalized_phone,
                            "conflict_user_ids": conflict_ids,
                            "source": source,
                            "conflict_at": datetime.utcnow().isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            return {"status": "conflict", "conflict_user_ids": conflict_ids}

        other = matches[0]
        primary_id, secondary_id = self.merge_users(
            db,
            user_a_id=user_id,
            user_b_id=other.id,
            reason="phone_verified",
            strategy="auto_by_phone",
        )
        return {
            "status": "merged",
            "primary_user_id": primary_id,
            "secondary_user_id": secondary_id,
        }

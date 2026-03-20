from __future__ import annotations

import json
from datetime import datetime

from dance_studio.auth.services.common import ensure_user_phone, normalize_phone_e164
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
    def score_user(self, db, user_id: int) -> int:
        return (
            db.query(BookingRequest).filter(BookingRequest.user_id == user_id).count() * 5
            + db.query(Attendance).filter(Attendance.user_id == user_id).count() * 3
            + db.query(GroupAbonement).filter(GroupAbonement.user_id == user_id).count() * 4
            + db.query(PaymentTransaction).filter(PaymentTransaction.user_id == user_id).count() * 4
        )

    def choose_primary_user(self, db, user_a_id: int, user_b_id: int) -> tuple[int, int]:
        score_a = self.score_user(db, user_a_id)
        score_b = self.score_user(db, user_b_id)
        if score_a == score_b:
            user_a = db.query(User).filter(User.id == user_a_id).first()
            user_b = db.query(User).filter(User.id == user_b_id).first()
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

        secondary = db.query(User).filter(User.id == secondary_id).first()
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
        normalized_phone = normalize_phone_e164(phone)
        if not normalized_phone:
            return {"status": "no_phone"}

        ensure_user_phone(
            db,
            user_id=user_id,
            phone_e164=normalized_phone,
            source=source,
            verified_at=datetime.utcnow(),
            is_primary=True,
        )

        matches = (
            db.query(UserPhone)
            .filter(
                UserPhone.phone_e164 == normalized_phone,
                UserPhone.verified_at.isnot(None),
                UserPhone.user_id != user_id,
            )
            .all()
        )
        match_user_ids = sorted({row.user_id for row in matches})

        if not match_user_ids:
            return {"status": "no_match"}

        if len(match_user_ids) > 1:
            db.add(
                UserMergeEvent(
                    source_user_id=user_id,
                    target_user_id=match_user_ids[0],
                    merge_reason="phone_conflict",
                    merge_strategy="auto_by_phone",
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

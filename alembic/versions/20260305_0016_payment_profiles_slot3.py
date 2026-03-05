"""Expand payment profile slots to 3 entries

Revision ID: 20260305_0016_pay_profiles3
Revises: 20260302_0015_user_discounts
Create Date: 2026-03-05
"""

from alembic import op


revision = "20260305_0016_pay_profiles3"
down_revision = "20260302_0015_user_discounts"
branch_labels = None
depends_on = None


def _upgrade_constraint() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payment_profiles", recreate="always") as batch:
            batch.drop_constraint("ck_payment_profiles_slot_range", type_="check")
            batch.create_check_constraint("ck_payment_profiles_slot_range", "slot in (1, 2, 3)")
        return
    op.drop_constraint("ck_payment_profiles_slot_range", "payment_profiles", type_="check")
    op.create_check_constraint("ck_payment_profiles_slot_range", "payment_profiles", "slot in (1, 2, 3)")


def _downgrade_constraint() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payment_profiles", recreate="always") as batch:
            batch.drop_constraint("ck_payment_profiles_slot_range", type_="check")
            batch.create_check_constraint("ck_payment_profiles_slot_range", "slot in (1, 2)")
        return
    op.drop_constraint("ck_payment_profiles_slot_range", "payment_profiles", type_="check")
    op.create_check_constraint("ck_payment_profiles_slot_range", "payment_profiles", "slot in (1, 2)")


def upgrade():
    _upgrade_constraint()
    op.execute(
        """
        INSERT INTO payment_profiles (
            slot,
            title,
            details,
            recipient_bank,
            recipient_number,
            recipient_full_name,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            3,
            'Реквизиты 3',
            '',
            '',
            '',
            '',
            FALSE,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_profiles WHERE slot = 3
        )
        """
    )
    # Keep owner-1 slot never active. Owner-2 defaults to slot 2 when needed.
    op.execute("UPDATE payment_profiles SET is_active = FALSE WHERE slot = 1")
    op.execute(
        """
        UPDATE payment_profiles
        SET is_active = CASE
            WHEN slot = 2 THEN TRUE
            WHEN slot = 3 THEN FALSE
            ELSE is_active
        END
        WHERE slot IN (2, 3)
          AND NOT EXISTS (
              SELECT 1
              FROM payment_profiles p2
              WHERE p2.slot IN (2, 3) AND p2.is_active = TRUE
          )
        """
    )
    op.execute(
        """
        UPDATE payment_profiles
        SET is_active = CASE
            WHEN slot = 2 THEN TRUE
            WHEN slot = 3 THEN FALSE
            ELSE is_active
        END
        WHERE slot IN (2, 3)
          AND (
              SELECT COUNT(*)
              FROM payment_profiles p2
              WHERE p2.slot IN (2, 3) AND p2.is_active = TRUE
          ) > 1
        """
    )


def downgrade():
    op.execute("DELETE FROM payment_profiles WHERE slot = 3")
    op.execute(
        """
        UPDATE payment_profiles
        SET is_active = TRUE
        WHERE slot = 1
          AND NOT EXISTS (
              SELECT 1
              FROM payment_profiles p2
              WHERE p2.slot IN (1, 2) AND p2.is_active = TRUE
          )
        """
    )
    _downgrade_constraint()

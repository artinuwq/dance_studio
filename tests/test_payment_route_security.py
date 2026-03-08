from flask import Flask

from dance_studio.web.routes.payments import bp as payments_bp


def test_user_payment_mark_paid_route_is_not_registered():
    app = Flask(__name__)
    app.register_blueprint(payments_bp)

    routes = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/api/payment-transactions/<int:payment_id>/pay" not in routes
    assert "/api/payment-transactions/my" in routes
    assert "/api/admin/payments" in routes
    assert "/api/admin/booking-requests/<int:booking_id>/confirm-payment" in routes
    assert "/api/admin/group-abonements/<int:abonement_id>/confirm-payment" in routes

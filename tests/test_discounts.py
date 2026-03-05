import pytest
from dance_studio.db.models import UserDiscount

def calculate_discounted_amount(base_amount, discounts):
    if not discounts:
        return base_amount
    
    best_discount_amt = 0
    for d in discounts:
        if not d.is_active:
            continue
        val = int(base_amount * (d.value / 100)) if d.discount_type == "percentage" else d.value
        if val > best_discount_amt:
            best_discount_amt = val
            
    return max(0, base_amount - best_discount_amt)

def test_percentage_discount():
    d = UserDiscount(discount_type="percentage", value=10, is_active=True)
    assert calculate_discounted_amount(5000, [d]) == 4500

def test_fixed_discount():
    d = UserDiscount(discount_type="fixed", value=500, is_active=True)
    assert calculate_discounted_amount(5000, [d]) == 4500

def test_multiple_discounts_picks_best():
    d1 = UserDiscount(discount_type="percentage", value=10, is_active=True) # 500 off
    d2 = UserDiscount(discount_type="fixed", value=1000, is_active=True)    # 1000 off
    assert calculate_discounted_amount(5000, [d1, d2]) == 4000

def test_inactive_discount_ignored():
    d = UserDiscount(discount_type="percentage", value=50, is_active=False)
    assert calculate_discounted_amount(5000, [d]) == 5000

def test_discount_not_below_zero():
    d = UserDiscount(discount_type="fixed", value=6000, is_active=True)
    assert calculate_discounted_amount(5000, [d]) == 0

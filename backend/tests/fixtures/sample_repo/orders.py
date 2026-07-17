"""Fixture: cross-file constructor call + instance-method call.
Note: `create` shares its name with stripe.PaymentIntent.create — the
resolver must NOT invent an edge from billing.py to this function."""

from billing import Billing


def create(order):
    b = Billing()
    return b.charge(order.user_id, order.total)

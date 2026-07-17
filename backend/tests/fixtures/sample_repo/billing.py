"""Fixture: inheritance + imported-symbol call + external (stripe) call."""

import stripe
from users import Base, fetch_user_tier


class Billing(Base):
    def charge(self, user_id, amount):
        tier = fetch_user_tier(user_id)
        self.save()
        return stripe.PaymentIntent.create(amount=amount, metadata={"tier": tier})

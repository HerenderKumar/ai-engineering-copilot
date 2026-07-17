"""Fixture: base class + a free function other files call."""


class Base:
    def save(self):
        return True


def fetch_user_tier(user_id):
    return "gold"

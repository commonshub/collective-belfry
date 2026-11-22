from belfry_onboarding import joined


class Relay:
    def __init__(self, seen=()):
        self.seen = list(seen)
        self.sent = []

    def query(self, filters, sign):
        self.asked = filters
        return self.seen

    def publish(self, event, sign):
        self.sent.append(event)


def _with(monkeypatch, relay):
    monkeypatch.setattr(joined, "_relay", lambda svc: (relay, lambda e: e))


def test_a_new_member_is_announced_in_the_main_channel(monkeypatch):
    relay = Relay()
    _with(monkeypatch, relay)
    assert joined.announce(None, "ab" * 32, "  Lina   Moreau ") is True
    [e] = relay.sent
    assert e["kind"] == 9000 and e["content"] == ""
    assert e["tags"] == [
        ["h", "general"],
        ["p", "ab" * 32, "member"],
        ["joined", "belfry"],
        ["name", "Lina Moreau"],
    ]
    assert relay.asked == [{"kinds": [9000], "#h": ["general"], "#p": ["ab" * 32]}]


def test_an_agent_is_announced_as_one(monkeypatch):
    relay = Relay()
    _with(monkeypatch, relay)
    joined.announce(None, "cd" * 32, "Garden bot", agent=True)
    assert ["agent", "1"] in relay.sent[0]["tags"]


def test_nobody_is_announced_twice(monkeypatch):
    # a guest's own 9000 in the main channel is not a join line of the belfry; an earlier line is
    guest_line = {"tags": [["h", "general"], ["p", "ab" * 32, "member"], ["guest", "2099-01-01"]]}
    relay = Relay([guest_line])
    _with(monkeypatch, relay)
    assert joined.announce(None, "ab" * 32, "Lina") is True
    relay.seen.append(relay.sent[0])
    assert joined.announce(None, "ab" * 32, "Lina") is False and len(relay.sent) == 1


def test_a_relay_that_fails_costs_only_the_line(monkeypatch):
    class Down(Relay):
        def query(self, filters, sign):
            raise TimeoutError("no answer")

    _with(monkeypatch, Down())
    assert joined.announce(None, "ab" * 32, "Lina") is False
    monkeypatch.setattr(joined, "_relay", lambda svc: None)
    assert joined.announce(None, "ab" * 32, "Lina") is False
    assert joined.announce(None, "", "Lina") is False

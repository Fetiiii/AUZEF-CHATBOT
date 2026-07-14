"""İstatistik agregasyonu: SQL sonuçları Python oracle'ıyla birebir aynı olmalı."""
import random
from datetime import datetime, timedelta

import pytest


def _seed(db, n=120):
    """Deterministik ama karışık veri: talep durumları, puanlar, timestamp çakışmaları."""
    from core.database import Conversation, ConversationMessage
    rnd = random.Random(42)
    base = datetime(2026, 6, 1, 10, 0, 0)
    statuses = ["not_offered", "declined", "redirected"]
    for i in range(n):
        st = base + timedelta(hours=rnd.randint(0, 24 * 40))
        conv = Conversation(ip_address=f"10.0.0.{i % 250}",
                            talep_status=rnd.choice(statuses),
                            client_token=f"t{i}", started_at=st)
        db.add(conv); db.flush()
        for j in range(rnd.randint(1, 5)):
            ts = st if rnd.random() < 0.3 else st + timedelta(minutes=j)  # kasıtlı tie
            db.add(ConversationMessage(
                conversation_id=conv.id, role="bot", content=f"c{i}m{j}",
                rating=rnd.choice([None, None, 1, 2, 3, 4, 5]), created_at=ts))
    db.commit()


def _oracle(db, start, end):
    """Eski (satırları Python'a çekip sayan) implementasyonun birebir kopyası."""
    from core.database import Conversation, ConversationMessage
    ISTANBUL_OFFSET = timedelta(hours=3)
    q = db.query(Conversation)
    if start:
        q = q.filter(Conversation.started_at >= datetime.strptime(start, "%Y-%m-%d") - ISTANBUL_OFFSET)
    if end:
        q = q.filter(Conversation.started_at < datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1) - ISTANBUL_OFFSET)
    convs = q.all()
    conv_ids = [c.id for c in convs]
    total = len(convs)
    talep = {"redirected": 0, "declined": 0, "not_offered": 0}
    for c in convs:
        if c.talep_status in talep:
            talep[c.talep_status] += 1
    dist = {str(i): 0 for i in range(1, 6)}
    last = {}
    if conv_ids:
        rated = (db.query(ConversationMessage)
                 .filter(ConversationMessage.conversation_id.in_(conv_ids),
                         ConversationMessage.rating.isnot(None))
                 .order_by(ConversationMessage.created_at, ConversationMessage.id).all())
        for m in rated:
            if 1 <= m.rating <= 5:
                dist[str(m.rating)] += 1
            last[m.conversation_id] = m.rating
    olumlu = sum(1 for r in last.values() if r >= 4)
    olumsuz = sum(1 for r in last.values() if r <= 3)
    return {"total_conversations": total, "rating_distribution": dist,
            "outcome": {"olumlu": olumlu, "olumsuz": olumsuz, "puansiz": total - len(last)},
            "talep": talep}


@pytest.mark.parametrize("start,end", [
    ("", ""), ("2026-06-10", ""), ("", "2026-06-20"), ("2026-06-05", "2026-07-01"),
])
def test_sql_aggregation_matches_python_oracle(db, start, end):
    from routers.stats import get_conversation_stats
    _seed(db)
    assert get_conversation_stats(start=start, end=end, db=db) == _oracle(db, start, end)


def test_invalid_dates_rejected(db):
    from routers.stats import get_conversation_stats
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        get_conversation_stats(start="dun", end="", db=db)
    with pytest.raises(HTTPException):
        get_conversation_stats(start="", end="31-12-2026", db=db)

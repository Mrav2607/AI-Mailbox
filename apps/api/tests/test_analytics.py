import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

from app.routes.analytics import analytics_overview


def test_overview_counts_only_non_null_classification_labels():
    user = MagicMock(id=uuid4())
    db = MagicMock()
    db.scalar.side_effect = [4, 7, 5]

    result = asyncio.run(analytics_overview(current_user=user, db=db))

    assert result == {
        "summary": {"threads": 4, "messages": 7, "classified": 5}
    }
    classification_query = db.scalar.call_args_list[2].args[0]
    assert "classification.label IS NOT NULL" in str(classification_query)

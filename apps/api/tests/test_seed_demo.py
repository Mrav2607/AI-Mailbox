"""Offline coverage for app/scripts/seed_demo.py.

test_seed_demo_integration.py already covers this against a real Postgres,
but that tier is opt-in via TEST_DATABASE_URL and skips in the default run --
so without this file, the seed has zero coverage in CI. seed_demo_data
chains real ORM instances (account.id -> thread.provider_account_id ->
thread.id -> message.thread_id), so a plain statement-recording fake isn't
enough here; _FakeSeedSession actually assigns ids on add() (mirroring every
model's own `default=uuid.uuid4`) and answers the two selects seed_demo_data
issues by dispatching on table name, mirroring test_backfill.py's
_BackfillFakeDB/test_extraction_run.py's _FakeSession table-dispatch idiom.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.db.models import ProviderAccount
from app.scripts.seed_demo import DEMO_MODEL_VERSION, LEGACY_DEMO_EXTERNAL_ID, seed_demo_data


class _Result:
    def __init__(self, items):
        self._items = list(items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _FakeSeedSession:
    """Just enough of a Session for seed_demo_data: add() drops a row into a
    per-table list and stamps an id (every model here defaults id to
    uuid.uuid4, so this mirrors what a real flush would assign), and
    execute() answers seed_demo_data's two selects -- the existing-account
    lookup and the idempotency check -- by dispatching on the target table,
    same idiom as _BackfillFakeDB in test_backfill.py.
    """

    def __init__(self):
        self.rows: dict[str, list] = {
            "provider_account": [],
            "mail_thread": [],
            "mail_message": [],
            "classification": [],
        }
        self.commits = 0

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        table = obj.__tablename__
        self.rows[table].append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def execute(self, stmt):
        # Both selects seed_demo_data issues are scoped to exactly one
        # account (the single demo user/account pair each test builds), so
        # handing back everything already added for that table is
        # equivalent to actually evaluating the WHERE clause -- same
        # "trust the test scenario, dispatch by table" idiom as
        # _BackfillFakeDB/test_extraction_run.py's _FakeSession, without
        # reaching into SQLAlchemy's private statement internals.
        table = stmt.selected_columns[0].table.name
        if table not in self.rows:
            raise AssertionError(f"unexpected table in fake session: {table}")
        return _Result(self.rows[table])


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="demo-user@example.com")


def test_seeded_account_has_no_refresh_token():
    # This is the property that keeps the periodic sync (and now the manual
    # ingest route) away from the placeholder connection -- see the
    # `refresh_token IS NOT NULL` predicate in tasks_ingest.py and mailbox.py.
    db = _FakeSeedSession()
    user = _user()

    seed_demo_data(db, user)

    accounts = db.rows["provider_account"]
    assert len(accounts) == 1
    assert accounts[0].refresh_token is None


def test_seeding_twice_does_not_duplicate_the_sample_inbox():
    db = _FakeSeedSession()
    user = _user()

    first = seed_demo_data(db, user)
    second = seed_demo_data(db, user)

    assert first > 0
    assert second == 0
    assert len(db.rows["provider_account"]) == 1
    assert len(db.rows["mail_thread"]) == first


def test_seeded_classifications_use_the_demo_model_version():
    db = _FakeSeedSession()
    user = _user()

    seed_demo_data(db, user)

    classifications = db.rows["classification"]
    assert classifications
    assert DEMO_MODEL_VERSION == "demo-seed"
    assert all(c.model_version == DEMO_MODEL_VERSION for c in classifications)


def test_seeding_two_different_users_produces_different_external_ids():
    # provider_account's (provider, external_user_id) unique constraint isn't
    # scoped by user_id (models/provider.py) -- a shared literal external id
    # made the SECOND demo user's insert collide with the first's row. Each
    # user needs their own external id so two users seeding never collide;
    # the actual IntegrityError-avoidance is covered against a real Postgres
    # in test_seed_demo_integration.py's cross-user test.
    user_a, user_b = _user(), _user()
    db_a, db_b = _FakeSeedSession(), _FakeSeedSession()

    seed_demo_data(db_a, user_a)
    seed_demo_data(db_b, user_b)

    account_a = db_a.rows["provider_account"][0]
    account_b = db_b.rows["provider_account"][0]
    assert account_a.external_user_id != account_b.external_user_id


def test_legacy_external_id_account_is_reused_not_duplicated():
    # A user seeded before the external id was scoped per-user has a
    # provider_account row under the old shared literal -- seeding again
    # must find and reuse that row, not insert a second placeholder account.
    db = _FakeSeedSession()
    user = _user()
    legacy_account = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id=LEGACY_DEMO_EXTERNAL_ID,
        access_token="demo-seed-no-token",
        refresh_token=None,
    )
    db.add(legacy_account)

    created = seed_demo_data(db, user)

    assert created > 0
    accounts = db.rows["provider_account"]
    assert len(accounts) == 1
    assert accounts[0].external_user_id == LEGACY_DEMO_EXTERNAL_ID
    assert all(t.provider_account_id == accounts[0].id for t in db.rows["mail_thread"])

"""Registration authority, invite lifetime, and atomic enrollment regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarlo.server import app, set_backend
from swarlo.sqlite_backend import SQLiteBackend
from swarlo.types import Member


@pytest.fixture
def backend(tmp_path):
    backend = SQLiteBackend(str(tmp_path / 'hub.db'))
    set_backend(backend)
    yield backend
    backend.close()


@pytest.fixture
def client(backend):
    return TestClient(app)


def enroll(client, member_id, hub='victim', member_type='agent', **options):
    return client.post('/api/register', json={
        'hub_id': hub, 'member_id': member_id, 'member_type': member_type,
        **options.pop('payload', {}),
    }, **options)


def auth(response):
    assert response.status_code == 201
    return {'Authorization': f"Bearer {response.json()['api_key']}"}


def invite(client, headers, hub='victim', **payload):
    response = client.post(f'/api/{hub}/invites', headers=headers, json=payload)
    assert response.status_code == 201
    return response.json()['invite_token']


@pytest.mark.parametrize('member_type', ['agent', 'human'])
def test_new_hub_is_open_but_existing_hub_requires_authority(client, backend, member_type):
    headers = auth(enroll(client, 'owner', member_type=member_type))
    assert client.post('/api/victim/channels/general/posts', headers=headers,
                       json={'content': 'Private hub content'}).status_code == 201
    denied = enroll(client, 'intruder')
    assert denied.status_code == 403
    assert 'api_key' not in denied.json()
    assert backend.get_member('victim', 'intruder') is None
    assert client.get('/api/victim/channels/general/posts').status_code == 401
    assert enroll(client, 'new-owner', hub='brand-new').status_code == 201


def test_invite_joins_once_and_can_read_its_hub(client, backend):
    headers = auth(enroll(client, 'owner'))
    token = invite(client, headers)
    joined = enroll(client, 'guest', payload={'invite_token': token})
    assert client.get('/api/victim/channels/general/posts', headers=auth(joined)).status_code == 200
    assert enroll(client, 'second-guest', payload={'invite_token': token}).status_code == 403
    assert backend.get_member('victim', 'second-guest') is None


def test_invite_requires_current_same_hub_member(client):
    headers = auth(enroll(client, 'owner'))
    stranger = auth(enroll(client, 'outsider', hub='other'))
    assert client.post('/api/victim/invites', json={}).status_code == 401
    assert client.post('/api/victim/invites', headers=stranger, json={}).status_code == 403
    assert enroll(client, 'intruder', headers=stranger).status_code == 403
    assert enroll(client, 'intruder', headers={'Authorization': 'Bearer invalid'}).status_code == 403
    assert enroll(client, 'sponsored', headers=headers).status_code == 201


def test_expired_invite_and_invalid_token_are_refused(client, backend):
    headers = auth(enroll(client, 'owner'))
    token = invite(client, headers)
    backend.conn.execute('UPDATE hub_invites SET expires_at = ?',
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    backend.conn.commit()
    for candidate in (token, 'invalid'):
        assert enroll(client, 'guest', payload={'invite_token': candidate}).status_code == 403
    assert backend.get_member('victim', 'guest') is None


@pytest.mark.parametrize('minutes', [0, -1, 10081])
def test_invite_expiry_is_bounded(client, minutes):
    headers = auth(enroll(client, 'owner'))
    assert client.post('/api/victim/invites', headers=headers,
                       json={'expires_in_minutes': minutes}).status_code == 422


def test_invite_hub_and_role_are_bound_and_failed_join_keeps_invite(client):
    headers = auth(enroll(client, 'owner'))
    auth(enroll(client, 'other-owner', hub='other'))
    token = invite(client, headers)
    assert enroll(client, 'guest', hub='other', payload={'invite_token': token}).status_code == 403
    assert enroll(client, 'guest', member_type='human', payload={'invite_token': token}).status_code == 403
    assert enroll(client, 'guest', payload={'invite_token': token}).status_code == 201


def test_duplicate_registration_does_not_consume_invite(client):
    headers = auth(enroll(client, 'owner'))
    token = invite(client, headers)
    assert enroll(client, 'owner', payload={'invite_token': token}).status_code == 409
    assert enroll(client, 'guest', payload={'invite_token': token}).status_code == 201


def test_only_human_can_sponsor_human_and_human_invites_work(client):
    agent = auth(enroll(client, 'agent'))
    assert client.post('/api/victim/invites', headers=agent,
                       json={'member_type': 'human'}).status_code == 403
    assert enroll(client, 'impostor', member_type='human', headers=agent).status_code == 403
    operator = auth(enroll(client, 'operator', hub='operated', member_type='human'))
    token = invite(client, operator, hub='operated', member_type='human')
    assert enroll(client, 'second', hub='operated', member_type='human',
                  payload={'invite_token': token}).status_code == 201
    assert enroll(client, 'third', hub='operated', member_type='human', headers=operator).status_code == 201


def test_removed_sponsor_key_and_invite_are_refused_even_after_reenrollment(client, backend):
    owner = auth(enroll(client, 'owner', member_type='human'))
    agent_response = enroll(client, 'agent', headers=owner)
    agent = auth(agent_response)
    token = invite(client, agent)
    assert client.delete('/api/victim/members/agent', headers=owner).status_code == 200
    assert enroll(client, 'intruder', headers=agent).status_code == 403
    assert enroll(client, 'intruder', payload={'invite_token': token}).status_code == 403
    assert enroll(client, 'agent', headers=owner).status_code == 201
    assert enroll(client, 'intruder', payload={'invite_token': token}).status_code == 403
    assert backend.get_member('victim', 'intruder') is None


def test_rotated_sponsor_key_is_refused(client):
    headers = auth(enroll(client, 'owner'))
    assert client.post('/api/victim/rotate-key', headers=headers).status_code == 200
    assert enroll(client, 'intruder', headers=headers).status_code == 403


def test_empty_hub_stays_closed_after_restart(client, backend):
    headers = auth(enroll(client, 'owner'))
    assert client.delete('/api/victim/members/owner', headers=headers).status_code == 200
    backend.close()
    assert enroll(client, 'intruder').status_code == 403


@pytest.mark.parametrize('keep_member', [True, False])
def test_existing_database_hubs_are_migrated(client, backend, keep_member):
    headers = auth(enroll(client, 'owner'))
    assert client.post('/api/victim/channels/general/posts', headers=headers,
                       json={'content': 'Existing history'}).status_code == 201
    if not keep_member:
        assert client.delete('/api/victim/members/owner', headers=headers).status_code == 200
    # Simulate the old schema without a durable hub registry.
    backend.conn.execute('DROP TABLE hubs')
    backend.conn.commit()
    backend.close()
    assert enroll(client, 'intruder').status_code == 403


def test_trusted_local_provisioning_bootstraps_first_human(client, backend):
    auth(enroll(client, 'agent'))
    backend.register_member(Member(member_id='operator', hub_id='victim',
                                   member_type='human', member_name='Operator'),
                            api_key='local-test-operator')
    operator = {'Authorization': 'Bearer local-test-operator'}
    assert client.post('/api/victim/prune', headers=operator, json={}).status_code == 200
    token = invite(client, operator, member_type='human')
    assert enroll(client, 'second', member_type='human', payload={'invite_token': token}).status_code == 201


@pytest.mark.parametrize('with_invite', [True, False])
def test_concurrent_registration_is_atomic_across_backends(client, backend, with_invite):
    if with_invite:
        headers = auth(enroll(client, 'owner'))
        token = invite(client, headers)
    else:
        token = None
    db_path = backend.db_path

    def join_member(member_id):
        other = SQLiteBackend(db_path)
        try:
            other.register_member(Member(member_id=member_id, hub_id='victim',
                                         member_type='agent', member_name=member_id),
                                  api_key=f'test-{member_id}', require_authority=True,
                                  invite_token=token)
            return 'joined'
        except PermissionError:
            return 'refused'
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(join_member, ['first', 'second']))
    assert sorted(outcomes) == ['joined', 'refused']


def test_invite_survives_restart_and_is_only_stored_as_digest(client, backend):
    headers = auth(enroll(client, 'owner'))
    token = invite(client, headers)
    stored = backend.conn.execute('SELECT token_hash FROM hub_invites').fetchone()[0]
    hashed = stored != token and len(stored) == 64
    assert hashed
    backend.close()
    assert enroll(client, 'guest', payload={'invite_token': token}).status_code == 201

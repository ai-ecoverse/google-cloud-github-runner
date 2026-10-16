"""Reconciler decisions use mocked GitHub and Compute Engine clients only."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.clients.github_client import GitHubReadError
from app.clients.gcloud_client import RunnerInsertError
from app.services.reconcile_service import ReconcileService, RepoScanCache, StockoutBackoff, reconcile_lock

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REPO = {
    'full_name': 'example/project', 'html_url': 'https://github.com/example/project',
    'owner': {'login': 'example', 'type': 'Organization', 'html_url': 'https://github.com/example'},
}


def job(minutes, job_id):
    return {'id': job_id, 'status': 'queued', 'labels': ['self-hosted', 'gcp-bench-8core'],
            'created_at': (NOW - timedelta(minutes=minutes)).isoformat()}


def vm(name, minutes=5, idle_since=None, job_id=None):
    labels = {'gha-owner': 'example', 'gha-repo': 'project', 'gha-runner': 'gcp-bench-8core'}
    if idle_since is not None:
        labels['gha-idle-since'] = str(int((NOW - timedelta(minutes=idle_since)).timestamp()))
    if job_id is not None:
        labels['gha-job'] = str(job_id)
    return SimpleNamespace(name=name, labels=labels,
                           creation_timestamp=(NOW - timedelta(minutes=minutes)).isoformat(),
                           label_fingerprint='fingerprint')


def service(jobs=(), vms=(), runners=()):
    github = MagicMock()
    github.get_installation_access_token.return_value = 'mock-token'
    github.list_installation_repositories.return_value = [REPO]
    github.list_queued_workflow_jobs.return_value = list(jobs)
    github.list_runners.return_value = list(runners)
    gcloud = MagicMock()
    gcloud.list_runner_instances.return_value = list(vms)
    webhook = MagicMock()
    webhook._handle_queued_job.return_value = 'gcp-runner-new'
    return ReconcileService(github=github, gcloud=gcloud, webhook=webhook,
                            now=NOW, cache=RepoScanCache(), backoff=StockoutBackoff()), github, gcloud, webhook


def test_demand_supply_and_grace_period():
    busy = vm('gcp-runner-busy', minutes=30)
    provisioning = vm('gcp-runner-provisioning')
    svc, _, gcloud, webhook = service(
        jobs=[job(4, 1), job(3, 2), job(1, 3)], vms=[busy, provisioning],
        runners=[{'name': busy.name, 'status': 'online', 'busy': True}],
    )
    result = svc.run()['labels']['org:example/gcp-bench-8core']
    assert result == {'demand': 2, 'supply': 1, 'attempted': 1, 'created': 1, 'deleted': 0, 'errors': {}}
    webhook._handle_queued_job.assert_called_once()
    gcloud.delete_runner_instance.assert_not_called()


def test_quota_error_stops_all_topups():
    svc, _, _, webhook = service(jobs=[job(4, 1), job(4, 2), job(4, 3)])
    webhook._handle_queued_job.side_effect = RuntimeError('QUOTA_EXCEEDED: CPUS_ALL_REGIONS')
    result = svc.run()
    assert result['capacity_stop'] is True
    assert result['labels']['org:example/gcp-bench-8core']['created'] == 0
    webhook._handle_queued_job.assert_called_once()


def test_old_online_idle_runner_is_reaped_after_fresh_check():
    idle = vm('gcp-runner-idle', minutes=60, idle_since=21)
    svc, github, gcloud, _ = service(
        vms=[idle], runners=[{'name': idle.name, 'status': 'online', 'busy': False}],
    )
    result = svc.run()['labels']['org:example/gcp-bench-8core']
    assert result['deleted'] == 1
    assert result['supply'] == 0
    assert github.list_runners.call_count == 2
    gcloud.delete_runner_instance.assert_called_once_with(idle.name, delivery_id='reconcile')


def test_first_idle_observation_starts_full_idle_window():
    idle = vm('gcp-runner-idle', minutes=60)
    svc, _, gcloud, _ = service(
        vms=[idle], runners=[{'name': idle.name, 'status': 'online', 'busy': False}],
    )
    assert svc.run()['labels']['org:example/gcp-bench-8core']['supply'] == 1
    gcloud.set_runner_idle_since.assert_called_once_with(idle, int(NOW.timestamp()))
    gcloud.delete_runner_instance.assert_not_called()


def test_never_registered_vm_is_reaped():
    missing = vm('gcp-runner-missing', minutes=16)
    svc, _, gcloud, _ = service(vms=[missing])
    assert svc.run()['labels']['org:example/gcp-bench-8core']['deleted'] == 1
    gcloud.delete_runner_instance.assert_called_once_with(missing.name, delivery_id='reconcile')


def test_busy_runner_is_never_deleted_even_with_old_idle_marker():
    busy = vm('gcp-runner-busy', minutes=60, idle_since=40)
    svc, _, gcloud, _ = service(
        vms=[busy], runners=[{'name': busy.name, 'status': 'online', 'busy': True}],
    )
    assert svc.run()['labels']['org:example/gcp-bench-8core']['deleted'] == 0
    gcloud.delete_runner_instance.assert_not_called()
    gcloud.set_runner_idle_since.assert_called_once_with(busy, None)


def test_runner_becomes_busy_before_delete():
    idle = vm('gcp-runner-idle', minutes=60, idle_since=21)
    svc, github, gcloud, _ = service(vms=[idle])
    github.list_runners.side_effect = [
        [{'name': idle.name, 'status': 'online', 'busy': False}],
        [{'name': idle.name, 'status': 'online', 'busy': True}],
    ]
    svc.run()
    gcloud.delete_runner_instance.assert_not_called()


def test_vm_registers_during_never_registered_recheck():
    new_runner = vm('gcp-runner-new', minutes=16)
    svc, github, gcloud, _ = service(vms=[new_runner])
    github.list_runners.side_effect = [
        [], [{'name': new_runner.name, 'status': 'online', 'busy': False}],
    ]
    svc.run()
    gcloud.delete_runner_instance.assert_not_called()


def test_zone_stockout_also_stops_topups():
    svc, _, _, webhook = service(jobs=[job(4, 1), job(4, 2)])
    webhook._handle_queued_job.side_effect = RuntimeError('ZONE_RESOURCE_POOL_EXHAUSTED')
    assert svc.run()['capacity_stop'] is True
    webhook._handle_queued_job.assert_called_once()


def test_recent_busy_assignment_covers_stale_queued_job():
    assigned = vm('gcp-runner-assigned', minutes=2, job_id=71)
    svc, _, _, webhook = service(
        jobs=[job(8, 71)], vms=[assigned],
        runners=[{'name': assigned.name, 'status': 'online', 'busy': True}],
    )
    result = svc.run()['labels']['org:example/gcp-bench-8core']
    assert result['demand'] == 0
    assert result['recent_assignments'] == 1
    webhook._handle_queued_job.assert_not_called()


def test_recent_provisioning_assignment_does_not_hide_other_job():
    assigned = vm('gcp-runner-assigned', minutes=2, job_id=71)
    svc, _, _, webhook = service(jobs=[job(8, 71), job(7, 72)], vms=[assigned])
    result = svc.run()['labels']['org:example/gcp-bench-8core']
    assert result['demand'] == 1
    assert result['supply'] == 0
    assert result['created'] == 1
    assert webhook._handle_queued_job.call_args.kwargs['job_id'] == '72'


def test_busy_assignment_older_than_grace_does_not_hide_stuck_job():
    assigned = vm('gcp-runner-assigned', minutes=6, job_id=71)
    svc, _, _, webhook = service(
        jobs=[job(8, 71)], vms=[assigned],
        runners=[{'name': assigned.name, 'status': 'online', 'busy': True}],
    )
    assert svc.run()['labels']['org:example/gcp-bench-8core']['created'] == 1
    webhook._handle_queued_job.assert_called_once()


def test_warm_tick_only_scans_hot_repos_and_vm_owners():
    repos = [REPO] + [
        {'full_name': f'example/other-{n}', 'html_url': f'https://github.com/example/other-{n}',
         'owner': REPO['owner']} for n in range(4)
    ]
    cache = RepoScanCache()
    svc, github, _, _ = service(jobs=[job(4, 71)])
    github.list_installation_repositories.return_value = repos
    github.list_queued_workflow_jobs.side_effect = lambda name, token: [job(4, 71)] if name == 'example/project' else []
    svc.cache = cache
    assert svc.run()['scanned_repos'] == 5
    github.list_queued_workflow_jobs.reset_mock()

    svc.now = NOW + timedelta(minutes=2)
    assert svc.run()['scanned_repos'] == 1
    assert github.list_queued_workflow_jobs.call_count == 1

    svc.now = NOW + timedelta(minutes=31)
    assert svc.run()['scanned_repos'] == 5


def test_warm_tick_scans_vm_owner_even_without_cached_job():
    cache = RepoScanCache()
    cache.last_full_scan = NOW - timedelta(minutes=2)
    repos = [REPO, {
        'full_name': 'example/unused', 'html_url': 'https://github.com/example/unused',
        'owner': REPO['owner'],
    }]
    owned_vm = vm('gcp-runner-provisioning', minutes=1)
    svc, github, gcloud, _ = service(vms=[owned_vm])
    svc.cache = cache
    github.list_installation_repositories.return_value = repos

    assert svc.run()['scanned_repos'] == 1
    github.list_queued_workflow_jobs.assert_called_once_with('example/project', 'mock-token')
    gcloud.delete_runner_instance.assert_not_called()


def test_overlapping_tick_skips_without_reading_or_mutating():
    svc, github, gcloud, webhook = service(jobs=[job(4, 1)])
    reconcile_lock.acquire()
    try:
        result = svc.run()
    finally:
        reconcile_lock.release()

    assert result['skipped'] == 'already_running'
    github.get_installation_access_token.assert_not_called()
    gcloud.list_runner_instances.assert_not_called()
    webhook._handle_queued_job.assert_not_called()


def test_failed_warm_scan_logs_safe_github_context_and_releases_lock(capsys):
    cache = RepoScanCache()
    cache.last_full_scan = NOW - timedelta(minutes=2)
    cache.hot_repos['example/project'] = NOW
    svc, github, _, _ = service()
    svc.cache = cache
    github.list_queued_workflow_jobs.side_effect = GitHubReadError(
        '/repos/example/project/actions/runs', 200, 'incomplete_pagination',
    )

    with pytest.raises(GitHubReadError):
        svc.run()

    result = json.loads(capsys.readouterr().out.strip())
    assert result['full_scan'] is False
    assert result['scanned_repos'] == 1
    assert result['error_status'] == 200
    assert result['error_endpoint'] == '/repos/example/project/actions/runs'
    assert 'github_jobs' in result['timing_ms']
    assert 'secret' not in str(result)
    assert reconcile_lock.locked() is False


def test_stockout_burst_probes_once_then_fans_out_after_recovery(capsys):
    svc, _, gcloud, webhook = service(jobs=[job(4, number) for number in range(1, 23)])
    webhook._handle_queued_job.side_effect = [
        RunnerInsertError('stockout', 'us-central1-f'),
        RunnerInsertError('stockout', 'us-central1-f'),
        *[f'gcp-runner-{number}' for number in range(22)],
    ]

    first = svc.run()
    second = svc.run()
    recovered = svc.run()

    assert first['demand'] == second['demand'] == recovered['demand'] == 22
    assert first['attempted'] == second['attempted'] == 1
    assert first['errors'] == second['errors'] == {'stockout': 1}
    assert first['capacity_stop'] is second['capacity_stop'] is True
    assert second['stockout_probe'] is True
    assert recovered['stockout_probe'] is True
    assert recovered['attempted'] == recovered['created'] == 22
    assert recovered['errors'] == {}
    assert svc.backoff.active is False
    assert webhook._handle_queued_job.call_count == 24
    gcloud.delete_runner_instance.assert_not_called()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3
    assert all(line['severity'] == 'INFO' for line in lines)
    assert lines[0]['labels']['org:example/gcp-bench-8core']['errors'] == {'stockout': 1}

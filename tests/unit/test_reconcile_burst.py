"""A 22-job stockout and recovery burst using mocked GitHub and GCE APIs."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.clients.gcloud_client import GCloudClient
from app.services.reconcile_service import ReconcileService, RepoScanCache, StockoutBackoff
from app.services.webhook_service import WebhookService


def test_stockout_burst_falls_back_probes_then_fans_out(monkeypatch):
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
    monkeypatch.delenv('GOOGLE_CLOUD_FALLBACK_ZONES', raising=False)
    now = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    repo = {
        'full_name': 'example/project', 'html_url': 'https://github.com/example/project',
        'owner': {'login': 'example', 'type': 'Organization', 'html_url': 'https://github.com/example'},
    }
    jobs = [
        {'id': number, 'created_at': (now - timedelta(minutes=4)).isoformat(),
         'labels': ['self-hosted', 'gcp-bench-8core'], 'status': 'queued'}
        for number in range(1, 23)
    ]
    github = MagicMock()
    github.get_installation_access_token.return_value = 'fake-installation-token'
    github.get_registration_token.return_value = 'fake-registration-token'
    github.list_installation_repositories.return_value = [repo]
    github.list_queued_workflow_jobs.return_value = jobs

    with patch('app.clients.gcloud_client.compute_v1.InstancesClient') as instances, \
            patch('app.clients.gcloud_client.compute_v1.RegionInstanceTemplatesClient'):
        gcloud = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        gcloud._get_template_name = MagicMock(return_value=template)
        instances.return_value.list.return_value = []
        failures = [MagicMock(error_code='ZONE_RESOURCE_POOL_EXHAUSTED') for _ in range(8)]
        for operation in failures:
            operation.result.side_effect = RuntimeError('stockout')
        successes = [MagicMock(error_code=None) for _ in range(22)]
        instances.return_value.insert.side_effect = failures + successes

        webhook = WebhookService.__new__(WebhookService)
        webhook.github_client = github
        webhook.gcloud_client = gcloud
        service = ReconcileService(github=github, gcloud=gcloud, webhook=webhook,
                                   now=now, cache=RepoScanCache(), backoff=StockoutBackoff())

        first = service.run()
        second = service.run()
        recovered = service.run()

        assert first['attempted'] == second['attempted'] == 1
        assert first['created'] == second['created'] == 0
        assert first['errors'] == second['errors'] == {'stockout': 1}
        assert recovered['attempted'] == recovered['created'] == 22
        assert recovered['errors'] == {}
        assert instances.return_value.insert.call_count == 30
        assert [call.kwargs['request'].zone for call in instances.return_value.insert.call_args_list[:8]] == [
            'us-central1-b', 'us-central1-a', 'us-central1-c', 'us-central1-f',
            'us-central1-b', 'us-central1-a', 'us-central1-c', 'us-central1-f',
        ]
        instances.return_value.delete.assert_not_called()

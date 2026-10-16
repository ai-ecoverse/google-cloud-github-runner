import pytest
import logging
from unittest.mock import patch, MagicMock
from app.clients.gcloud_client import GCloudClient, RunnerInsertError


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for GCloud client."""
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')


@pytest.fixture
def mock_compute_clients():
    """Mock the Compute Engine clients."""
    with patch('app.clients.gcloud_client.compute_v1.InstancesClient') as mock_instances, \
         patch('app.clients.gcloud_client.compute_v1.RegionInstanceTemplatesClient') as mock_templates:
        yield mock_instances, mock_templates


@pytest.fixture
def mock_gcloud_auth():
    """Mock Google Cloud authentication to prevent credential errors."""
    with patch('google.auth.default', return_value=(MagicMock(), 'test-project')):
        yield


class TestGCloudClient:
    def test_stockout_operation_falls_back_and_waits_for_success(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        monkeypatch.delenv('GOOGLE_CLOUD_FALLBACK_ZONES', raising=False)
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock(name='gcp-bench-8core-20260929120000')
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        client._get_template_name = MagicMock(return_value=template)
        stocked_out = MagicMock(error_code='ZONE_RESOURCE_POOL_EXHAUSTED')
        succeeded = MagicMock(error_code=None)
        next_success = MagicMock(error_code=None)
        instance_client.return_value.insert.side_effect = [stocked_out, succeeded, next_success]

        name = client.create_runner_instance('fake-token', 'https://github.com/example',
                                             'gcp-bench-8core', 'example/project', job_id=12345)
        client.create_runner_instance('fake-token', 'https://github.com/example',
                                      'gcp-bench-8core', 'example/project', job_id=12346)

        requests = [call.kwargs['request'] for call in instance_client.return_value.insert.call_args_list]
        assert [request.zone for request in requests] == ['us-central1-b', 'us-central1-a', 'us-central1-a']
        assert name.startswith('gcp-runner-us-central1-a-')
        assert requests[1].instance_resource.labels['gha-job'] == '12345'
        assert stocked_out.result.call_count == 1
        succeeded.result.assert_called_once_with(timeout=90)

    def test_all_zones_stockout_is_reported_after_four_failed_operations(
        self, monkeypatch, mock_compute_clients, mock_gcloud_auth,
    ):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        monkeypatch.delenv('GOOGLE_CLOUD_FALLBACK_ZONES', raising=False)
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        client._get_template_name = MagicMock(return_value=template)
        operations = [MagicMock(error_code='ZONE_RESOURCE_POOL_EXHAUSTED') for _ in range(4)]
        for operation in operations:
            operation.result.side_effect = RuntimeError('stockout')
        instance_client.return_value.insert.side_effect = operations

        with pytest.raises(RunnerInsertError, match='reason=stockout'):
            client.create_runner_instance('fake-token', 'https://github.com/example',
                                          'gcp-bench-8core', 'example/project', job_id=12345)

        requests = [call.kwargs['request'] for call in instance_client.return_value.insert.call_args_list]
        assert [request.zone for request in requests] == [
            'us-central1-b', 'us-central1-a', 'us-central1-c', 'us-central1-f',
        ]

    def test_quota_operation_stops_without_zone_fallback(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        client._get_template_name = MagicMock(return_value=template)
        operation = MagicMock(error_code='QUOTA_EXCEEDED')
        operation.result.side_effect = RuntimeError('quota')
        instance_client.return_value.insert.return_value = operation

        with pytest.raises(RunnerInsertError, match='reason=quota'):
            client.create_runner_instance('fake-token', 'https://github.com/example',
                                          'gcp-bench-8core', 'example/project', job_id=12345)
        instance_client.return_value.insert.assert_called_once()

    def test_webhook_returns_without_waiting_for_insert_operation(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        client._get_template_name = MagicMock(return_value=template)

        client.create_runner_instance('fake-token', 'https://github.com/example',
                                      'gcp-bench-8core', 'example/project')

        instance_client.return_value.insert.assert_called_once()
        instance_client.return_value.insert.return_value.result.assert_not_called()

    def test_fallback_vm_cleanup_and_idle_label_use_encoded_zone(
        self, monkeypatch, mock_compute_clients, mock_gcloud_auth,
    ):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        name = 'gcp-runner-us-central1-f-1234567890abcdef'
        vm = MagicMock(name=name)
        vm.name = name
        vm.labels = {'gha-runner': 'gcp-bench-8core'}
        vm.label_fingerprint = 'fingerprint'

        client.delete_runner_instance(name)
        client.set_runner_idle_since(vm, 12345)

        assert instance_client.return_value.delete.call_args.kwargs['zone'] == 'us-central1-f'
        assert instance_client.return_value.set_labels.call_args.kwargs['zone'] == 'us-central1-f'
        assert client._zone_for_name('gcp-runner-legacy123') == 'us-central1-b'

    def test_fallback_vm_stays_discoverable_if_fallback_config_is_disabled(
        self, monkeypatch, mock_compute_clients, mock_gcloud_auth,
    ):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        monkeypatch.setenv('GOOGLE_CLOUD_FALLBACK_ZONES', '')
        client = GCloudClient()

        assert client.zones == ('us-central1-b',)
        assert client.inventory_zones == ('us-central1-b', 'us-central1-a', 'us-central1-c', 'us-central1-f')
        assert client._zone_for_name('gcp-runner-us-central1-f-1234567890abcdef') == 'us-central1-f'

    def test_inventory_reads_all_configured_zones(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        instance_client, _ = mock_compute_clients
        client = GCloudClient()

        def list_zone(project, zone):
            vm = MagicMock()
            vm.name = f'gcp-runner-{zone}-1234567890abcdef'
            return [vm]

        instance_client.return_value.list.side_effect = list_zone

        assert len(client.list_runner_instances()) == 4
        assert [call.kwargs['zone'] for call in instance_client.return_value.list.call_args_list] == list(client.zones)

    def test_reconciler_job_id_becomes_gce_safe_label(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/example'
        client._get_template_name = MagicMock(return_value=template)

        client.create_runner_instance('fake-token', 'https://github.com/example',
                                      'gcp-bench-8core', 'example/project', job_id='12345678901')

        request = instance_client.return_value.insert.call_args.kwargs['request']
        assert request.instance_resource.labels['gha-job'] == '12345678901'
        assert request.instance_resource.labels['gha-runner'] == 'gcp-bench-8core'

    def test_reconciler_rejects_unsafe_job_id(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        template = MagicMock()
        template.name = 'gcp-bench-8core-20260929120000'
        client._get_template_name = MagicMock(return_value=template)

        with pytest.raises(ValueError, match='GCE-safe'):
            client.create_runner_instance('fake-token', 'https://github.com/example',
                                          'gcp-bench-8core', 'example/project', job_id='123/456')
        instance_client.return_value.insert.assert_not_called()

    def test_idle_marker_preserves_other_vm_labels(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        instance_client, _ = mock_compute_clients
        client = GCloudClient()
        vm = MagicMock()
        vm.name = 'gcp-runner-abc'
        vm.labels = {'gha-runner': 'gcp-bench-8core'}
        vm.label_fingerprint = 'fingerprint'

        client.set_runner_idle_since(vm, 123456)

        request = instance_client.return_value.set_labels.call_args.kwargs
        assert request['instance'] == vm.name
        assert dict(request['instances_set_labels_request_resource'].labels) == {
            'gha-runner': 'gcp-bench-8core', 'gha-idle-since': '123456',
        }

    def test_init_with_env_vars(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with environment variables."""
        client = GCloudClient()

        assert client.project_id == 'test-project'
        assert client.zone == 'us-central1-a'
        assert client.github_runner_group == ''
        assert client.region == 'us-central1'

    def test_init_default_zone(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with default zone."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.delenv('GOOGLE_CLOUD_ZONE', raising=False)

        client = GCloudClient()

        assert client.zone == 'us-central1-a'
        assert client.region == 'us-central1'

    def test_init_with_runner_group(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with runner group."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        client = GCloudClient()

        assert client.github_runner_group == 'platform-runners'

    def test_init_missing_project_id(self, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with missing project ID."""
        with patch.dict('os.environ', {}, clear=True):
            client = GCloudClient()
            assert client.project_id is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance(self, mock_compute, mock_env_vars):
        """Test creating a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        instance_name = client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        assert instance_name.startswith('gcp-runner-')
        mock_instance_client.insert.assert_called_once()

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert 'sudo -u runner ./config.sh' in startup_script
        assert 'sudo -u runner ./run.sh' in startup_script
        assert '--runnergroup' not in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_with_runner_group(self, mock_compute, monkeypatch, mock_env_vars):
        """Test creating a runner instance with runner group."""
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert '--runnergroup platform-runners' in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when creating instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with pytest.raises(RunnerInsertError, match="reason=other"):
            client.create_runner_instance(
                'fake-token',
                'https://github.com/owner/repo',
                'gcp-ubuntu-24.04'
            )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance(self, mock_compute, mock_env_vars):
        """Test deleting a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'delete-operation-123'
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('runner-12345')

        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='us-central1-a',
            instance='runner-12345'
        )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when deleting instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with pytest.raises(Exception, match="Delete Error"):
            client.delete_runner_instance('runner-12345')

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_found(self, mock_compute, mock_env_vars):
        """Test finding a template by prefix."""
        mock_templates_client = MagicMock()
        mock_template1 = MagicMock()
        mock_template1.name = 'other-template'
        mock_template2 = MagicMock()
        mock_template2.name = 'gcp-target-template-123456789012345'

        mock_templates_client.list.return_value = [mock_template1, mock_template2]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('gcp-target-template')

        assert result.name == 'gcp-target-template-123456789012345'

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_not_found(self, mock_compute, mock_env_vars):
        """Test not finding a template."""
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'other-template'

        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('non-existent')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_exception(self, mock_compute, mock_env_vars):
        """Test handling exception when getting template."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.side_effect = Exception("API Error")
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('any-template')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_no_template(self, mock_compute, mock_env_vars):
        """Test creating runner instance when no template is found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        result = client.create_runner_instance(
            'fake-token',
            'https://github.com/owner/repo',
            'non-existent-template'
        )

        assert result is None
        mock_instance_client.insert.assert_not_called()


class TestGCloudClientDeliveryIdLogging:
    """Tests to verify that delivery_id is logged in GCloudClient methods."""

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when creating an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "operation-123"
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "gcp-ubuntu-24.04",
                delivery_id="gce-create-delivery-001",
            )

        delivery_logs = [
            r for r in caplog.records if "gce-create-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (template match + creating + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when deleting an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "delete-operation-123"
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.delete_runner_instance(
                "runner-12345", delivery_id="gce-delete-delivery-001"
            )

        delivery_logs = [
            r for r in caplog.records if "gce-delete-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (deleting + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_no_template_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when no matching template found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.WARNING, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "non-existent",
                delivery_id="gce-notemplate-delivery-001",
            )

        assert any(
            "gce-notemplate-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in warning log for missing template"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance creation fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(RunnerInsertError, match="reason=other"):
                client.create_runner_instance(
                    "fake-token",
                    "https://github.com/owner/repo",
                    "gcp-ubuntu-24.04",
                    delivery_id="gce-error-delivery-001",
                )

        assert any(
            "gce-error-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance creation failure"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance deletion fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(Exception, match="Delete Error"):
                client.delete_runner_instance(
                    "runner-12345", delivery_id="gce-delerr-delivery-001"
                )

        assert any(
            "gce-delerr-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance deletion failure"

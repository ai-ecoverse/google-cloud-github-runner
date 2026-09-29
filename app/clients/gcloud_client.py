"""
Google Cloud Client for managing GCE instances.
"""
import logging
import os
import re
import uuid
import shlex
from concurrent.futures import TimeoutError as OperationTimeout
import google.cloud.compute_v1 as compute_v1

logger = logging.getLogger(__name__)


def insert_error_reason(error):
    """Classify capacity failures without copying provider messages into logs."""
    if isinstance(error, RunnerInsertError):
        return error.reason
    message = str(error).upper()
    if 'QUOTA_EXCEEDED' in message:
        return 'quota'
    if any(marker in message for marker in (
        'RESOURCE_POOL_EXHAUSTED', 'STOCKOUT', 'DOES NOT HAVE ENOUGH RESOURCES',
    )):
        return 'stockout'
    return 'other'


class RunnerInsertError(RuntimeError):
    """A safe, categorized failure from a VM insertion operation."""

    def __init__(self, reason, zone):
        self.reason = reason
        self.zone = zone
        super().__init__(f'runner insert failed: reason={reason} zone={zone}')


class GCloudClient:
    """Client for interacting with Google Cloud Compute Engine API."""

    def __init__(self):
        """Initialize GCloudClient with project and zone configuration."""
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
        self.zone = os.environ.get('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        self.github_runner_group = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
        self.region = '-'.join(self.zone.split('-')[:-1])
        default_fallbacks = 'us-central1-a,us-central1-c,us-central1-f' if self.zone == 'us-central1-b' else ''
        fallback_zones = os.environ.get('GOOGLE_CLOUD_FALLBACK_ZONES', default_fallbacks)
        self.zones = tuple(dict.fromkeys([self.zone] + [zone.strip() for zone in fallback_zones.split(',') if zone.strip()]))
        self.preferred_zone = self.zone
        known_zones = [f'us-central1-{suffix}' for suffix in ('a', 'b', 'c', 'f')] if self.region == 'us-central1' else []
        self.inventory_zones = tuple(dict.fromkeys(
            list(self.zones) + known_zones,
        ))
        if any(not re.fullmatch(rf'{re.escape(self.region)}-[a-z]', zone) for zone in self.zones):
            raise ValueError('Fallback zones must be in the configured region')

        if not self.project_id:
            logger.warning("GOOGLE_CLOUD_PROJECT not set. GCloudClient will not work correctly.")

        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
        self.instance_client = compute_v1.InstancesClient()
        # Create a RegionInstanceTemplatesClient for retrieving templates in a specific region
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.region_instance_templates
        self.instance_templates_client = compute_v1.RegionInstanceTemplatesClient()

    def _get_template_name(self, template_name):
        """
        Find a matching instance template by name prefix.

        Args:
            template_name (str): The name prefix to search for.

        Returns:
            google.cloud.compute_v1.InstanceTemplate or None: The matching template resource.
        """
        # Replace dots with dashes for template name, so gcp-ubuntu-24.04 matches gcp-ubuntu-24-04
        prefix = template_name.replace('.', '-')
        # logger.info(f"Prefix: {prefix}")
        # Create regex pattern: prefix followed by dash, at least 12 digits, and optional alphanumeric characters
        pattern = re.compile(f"^{re.escape(prefix)}-\\d{{14,}}[a-z0-9]*$")
        try:
            # List all templates to find one that matches the pattern
            for template in self.instance_templates_client.list(project=self.project_id, region=self.region):
                # logger.info(f"Template: {template.name}")
                if pattern.match(template.name):
                    return template
            return None
        except Exception:
            return None

    def create_runner_instance(
        self,
        registration_token,
        repo_url,
        template_name,
        instance_label=None,
        delivery_id=None,
        job_id=None,
    ):
        """
        Create a new GCE instance for a GitHub Actions runner.

        Args:
            registration_token (str): The GitHub Actions runner registration token.
            repo_url (str): The URL of the repository or organization.
            template_name (str): The name of the instance template to use.
            instance_label (str): Label to add to the Instance for Cost Tracking.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
            job_id (int or None): Queued GitHub job represented by a reconciler insert.

        Returns:
            str: The name of the created instance.
        """
        instance_template_resource = self._get_template_name(template_name)
        if instance_template_resource:
            logger.info(
                "Found matching instance template: %s, delivery_id: %s",
                instance_template_resource.name,
                delivery_id,
            )
        else:
            logger.warning(
                "No matching instance template found for label '%s' in region %s. "
                "Skipping instance creation. delivery_id: %s",
                template_name,
                self.region,
                delivery_id,
            )
            return None

        if job_id is not None and not re.fullmatch(r'[0-9]{1,63}', str(job_id)):
            raise ValueError("job_id must be a GCE-safe decimal label")

        runner_group_flag = ""
        if self.github_runner_group:
            runner_group_flag = f" --runnergroup {shlex.quote(self.github_runner_group)}"
        # Webhook deliveries return as soon as GCE accepts the operation. The
        # reconciler (which passes job_id) can wait and try fallback zones.
        zones = self.zones[:1]
        if job_id is not None:
            zones = (self.preferred_zone,) + tuple(zone for zone in self.zones if zone != self.preferred_zone)
        for zone in zones:
            # Encoding the zone in the runner name lets completion webhooks delete
            # the VM without a cross-zone lookup or a process-local name cache.
            prefix = 'gcp-runner-dependabot' if instance_template_resource.name.startswith('dependabot') else 'gcp-runner'
            instance_name = f'{prefix}-{zone}-{uuid.uuid4().hex[:16]}'
            instance_resource = compute_v1.Instance()
            instance_resource.name = instance_name
            if instance_label is not None:
                owner, repo = instance_label.split('/')
                instance_resource.labels = {
                    'gha-owner': owner.lower(), 'gha-repo': repo.lower(), 'gha-runner': template_name,
                }
                if job_id is not None:
                    instance_resource.labels['gha-job'] = str(job_id)

            startup_script = (
                "cd /actions-runner && "
                f"sudo -u runner ./config.sh --url {shlex.quote(repo_url)} "
                f"--token {shlex.quote(registration_token)} "
                f"--name {shlex.quote(instance_name)} "
                f"--labels {shlex.quote(template_name)} "
                f"{runner_group_flag} "
                "--ephemeral --unattended --no-default-labels --disableupdate && "
                "sudo -u runner ./run.sh"
            )
            metadata = compute_v1.Metadata()
            metadata.items = [
                compute_v1.Items(key='startup-script', value=startup_script),
                compute_v1.Items(key='vmDnsSetting', value='ZonalOnly'),
                compute_v1.Items(key='block-project-ssh-keys', value='true'),
            ]
            instance_resource.metadata = metadata
            request = compute_v1.InsertInstanceRequest(
                project=self.project_id, zone=zone, instance_resource=instance_resource,
                source_instance_template=instance_template_resource.self_link,
            )
            operation = None
            try:
                operation = self.instance_client.insert(request=request)
                if job_id is None:
                    logger.info('Runner insert operation started: name=%s zone=%s delivery_id=%s',
                                instance_name, zone, delivery_id)
                    return instance_name
                # insert() only starts an operation; stockouts appear on its result.
                operation.result(timeout=90)
                error_code = operation.error_code
                if isinstance(error_code, str) and error_code:
                    raise RunnerInsertError(insert_error_reason(error_code), zone)
                logger.info('Runner instance created: name=%s zone=%s delivery_id=%s',
                            instance_name, zone, delivery_id)
                self.preferred_zone = zone
                return instance_name
            except OperationTimeout as error:
                logger.error('Runner insert outcome unknown: zone=%s reason=timeout delivery_id=%s', zone, delivery_id)
                raise RunnerInsertError('timeout', zone) from error
            except Exception as error:
                error_code = getattr(operation, 'error_code', None)
                reason = insert_error_reason(error_code if isinstance(error_code, str) and error_code else error)
                log_failure = logger.warning if reason in ('stockout', 'quota') else logger.error
                log_failure('Runner insert failed: zone=%s reason=%s delivery_id=%s', zone, reason, delivery_id)
                if reason == 'stockout' and zone != zones[-1]:
                    continue
                raise RunnerInsertError(reason, zone) from error

    def _zone_for_name(self, instance_name):
        match = re.fullmatch(r'gcp-runner-(?:dependabot-)?([a-z0-9]+-[a-z0-9]+-[a-z])-[0-9a-f]{16}', instance_name)
        if match and match.group(1).startswith(f'{self.region}-'):
            return match.group(1)
        # VMs created before zone fallback carry no zone in their name.
        return self.zone

    def delete_runner_instance(self, instance_name, delivery_id=None):
        """
        Delete a GCE instance.

        Args:
            instance_name (str): The name of the instance to delete.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
        """
        logger.info(
            "Deleting GCE instance %s, delivery_id: %s", instance_name, delivery_id
        )
        try:
            operation = self.instance_client.delete(
                project=self.project_id,
                zone=self._zone_for_name(instance_name),
                instance=instance_name
            )
            logger.info(
                "Instance deletion operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
        except Exception as e:
            logger.error(
                "Failed to delete instance %s: %s, delivery_id: %s",
                instance_name,
                e,
                delivery_id,
            )
            raise

    def list_runner_instances(self):
        """Return manager-owned VMs across configured zones, including provisioning VMs."""
        return [instance for zone in self.inventory_zones
                for instance in self.instance_client.list(project=self.project_id, zone=zone)
                if instance.name.startswith('gcp-runner-')]

    def set_runner_idle_since(self, instance, timestamp):
        """Persist first observed idle time across Cloud Run instances and restarts."""
        labels = dict(instance.labels or {})
        if timestamp is None:
            labels.pop('gha-idle-since', None)
        else:
            labels['gha-idle-since'] = str(timestamp)
        self.instance_client.set_labels(
            project=self.project_id, zone=self._zone_for_name(instance.name), instance=instance.name,
            instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                labels=labels, label_fingerprint=instance.label_fingerprint,
            ),
        )

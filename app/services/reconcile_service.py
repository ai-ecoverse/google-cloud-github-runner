"""Periodic reconciliation of queued GitHub jobs and available GCE runners."""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from app.clients import GCloudClient, GitHubClient
from app.services.webhook_service import WebhookService


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def is_capacity_error(error):
    message = str(error).upper()
    return any(marker in message for marker in (
        'QUOTA_EXCEEDED', 'RESOURCE_POOL_EXHAUSTED', 'STOCKOUT',
        'DOES NOT HAVE ENOUGH RESOURCES', 'ZONE_RESOURCE_POOL',
    ))


def scope_for_repo(repo):
    owner = repo['owner']
    if owner.get('type') == 'Organization':
        return ('org', owner['login'].lower())
    return ('repo', repo['full_name'].lower())


def label_entry(summary, scope, label):
    key = f'{scope[0]}:{scope[1]}/{label}'
    return summary['labels'].setdefault(key, {'demand': 0, 'supply': 0, 'created': 0, 'deleted': 0})


class ReconcileService:
    """Top up missing capacity and retire runners that are safely idle."""

    def __init__(self, github=None, gcloud=None, webhook=None, now=None):
        self.github = github or GitHubClient()
        self.gcloud = gcloud or GCloudClient()
        self.webhook = webhook or WebhookService()
        self.now = now or datetime.now(timezone.utc)
        self.job_grace = int(os.environ.get('RECONCILE_JOB_GRACE_SECONDS', '120'))
        self.idle_limit = int(os.environ.get('RECONCILE_IDLE_SECONDS', '1200'))
        self.registration_limit = int(os.environ.get('RECONCILE_REGISTRATION_SECONDS', '900'))

    def _vm_scope(self, vm, scopes):
        labels = vm.labels or {}
        owner = labels.get('gha-owner', '').lower()
        repo = labels.get('gha-repo', '').lower()
        org_scope = ('org', owner)
        repo_scope = ('repo', f'{owner}/{repo}')
        if org_scope in scopes:
            return org_scope
        if repo_scope in scopes:
            return repo_scope
        return None

    def _can_delete(self, scope, name, token, was_registered):
        """Read GitHub once more immediately before deleting a VM."""
        runner = next((runner for runner in self.github.list_runners(scope, token)
                       if runner['name'] == name), None)
        if runner is None:
            return True
        return (was_registered and runner.get('status') == 'online'
                and runner.get('busy') is False)

    def _inventory(self, scopes, token, summary):
        runners = {scope: {runner['name']: runner for runner in self.github.list_runners(scope, token)}
                   for scope in scopes}
        supply = defaultdict(int)
        now_epoch = int(self.now.timestamp())
        for vm in self.gcloud.list_runner_instances():
            scope = self._vm_scope(vm, scopes)
            label = (vm.labels or {}).get('gha-runner')
            if not scope or not label:
                continue
            key = (scope, label)
            entry = label_entry(summary, scope, label)
            runner = runners[scope].get(vm.name)
            idle_since = (vm.labels or {}).get('gha-idle-since')
            if runner:
                if runner.get('busy') is not False or runner.get('status') != 'online':
                    if idle_since:
                        self.gcloud.set_runner_idle_since(vm, None)
                    continue
                if not idle_since:
                    self.gcloud.set_runner_idle_since(vm, now_epoch)
                    supply[key] += 1
                    continue
                if now_epoch - int(idle_since) < self.idle_limit:
                    supply[key] += 1
                    continue
            else:
                age = (self.now - parse_time(vm.creation_timestamp)).total_seconds()
                if age < self.registration_limit:
                    supply[key] += 1
                    continue
            if self._can_delete(scope, vm.name, token, runner is not None):
                self.gcloud.delete_runner_instance(vm.name, delivery_id='reconcile')
                entry['deleted'] += 1
        return supply

    def run(self):
        summary = {'event': 'reconcile', 'labels': {}, 'capacity_stop': False}
        try:
            token = self.github.get_installation_access_token()
            repos = self.github.list_installation_repositories(token)
            scopes = {scope_for_repo(repo) for repo in repos}
            demand = defaultdict(list)
            for repo in repos:
                scope = scope_for_repo(repo)
                for job in self.github.list_queued_workflow_jobs(repo['full_name'], token):
                    age = (self.now - parse_time(job['created_at'])).total_seconds()
                    if age < self.job_grace:
                        continue
                    label = next((label for label in job.get('labels', []) if label.startswith('gcp-')), None)
                    if label:
                        demand[(scope, label)].append((job['created_at'], repo))

            supply = self._inventory(scopes, token, summary)
            for key, count in supply.items():
                scope, label = key
                summary['labels'][f'{scope[0]}:{scope[1]}/{label}']['supply'] = count
            for key, jobs in sorted(demand.items()):
                scope, label = key
                entry = label_entry(summary, scope, label)
                entry['demand'] = len(jobs)
                entry['supply'] = supply[key]
                for _, repo in sorted(jobs, key=lambda item: item[0])[supply[key]:]:
                    owner = repo['owner']
                    try:
                        name = self.webhook._handle_queued_job(
                            label, repo['html_url'], owner['html_url'], repo['full_name'],
                            owner['login'] if scope[0] == 'org' else None,
                            delivery_id='reconcile',
                        )
                    except Exception as error:
                        if is_capacity_error(error):
                            summary['capacity_stop'] = True
                            return summary
                        raise
                    if not name:
                        entry['unsupported_template'] = True
                        break
                    entry['created'] += 1
            return summary
        except Exception as error:
            summary['error'] = type(error).__name__
            raise
        finally:
            print(json.dumps(summary, sort_keys=True), flush=True)

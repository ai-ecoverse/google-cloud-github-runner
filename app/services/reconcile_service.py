"""Periodic reconciliation of queued GitHub jobs and available GCE runners."""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

from app.clients import GCloudClient, GitHubClient
from app.clients.gcloud_client import insert_error_reason
from app.services.webhook_service import WebhookService


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def is_capacity_error(error):
    return insert_error_reason(error) in ('quota', 'stockout', 'timeout')


def scope_for_repo(repo):
    owner = repo['owner']
    if owner.get('type') == 'Organization':
        return ('org', owner['login'].lower())
    return ('repo', repo['full_name'].lower())


def label_entry(summary, scope, label):
    key = f'{scope[0]}:{scope[1]}/{label}'
    return summary['labels'].setdefault(key, {
        'demand': 0, 'supply': 0, 'attempted': 0, 'created': 0, 'deleted': 0, 'errors': {},
    })


def log_tick(summary):
    summary['demand'] = sum(entry['demand'] for entry in summary['labels'].values())
    summary['supply'] = sum(entry['supply'] for entry in summary['labels'].values())
    summary['deleted'] = sum(entry['deleted'] for entry in summary['labels'].values())
    print(json.dumps({'severity': 'INFO', **summary}, sort_keys=True), flush=True)


class StockoutBackoff:
    """Use one probe on the next tick after all configured zones stock out."""

    def __init__(self):
        self.active = False


class RepoScanCache:
    """Keep recently active repos warm; rescan the full installation periodically."""

    def __init__(self):
        self.last_full_scan = None
        self.hot_repos = {}

    def select(self, repos, vms, now, full_interval, hot_interval):
        full = (self.last_full_scan is None
                or (now - self.last_full_scan).total_seconds() >= full_interval)
        vm_repos = {
            f"{vm.labels['gha-owner']}/{vm.labels['gha-repo']}".lower()
            for vm in vms if vm.name.startswith('gcp-runner-')
            and vm.labels and vm.labels.get('gha-owner') and vm.labels.get('gha-repo')
        }
        self.hot_repos = {name: seen for name, seen in self.hot_repos.items()
                          if (now - seen).total_seconds() < hot_interval}
        if full:
            return repos, True
        return [repo for repo in repos if repo['full_name'].lower() in vm_repos
                or repo['full_name'].lower() in self.hot_repos], False

    def observe(self, repo_name, jobs, now):
        if any(any(label.startswith('gcp-') for label in job.get('labels', [])) for job in jobs):
            self.hot_repos[repo_name.lower()] = now

    def complete(self, now, full):
        if full:
            self.last_full_scan = now


repo_scan_cache = RepoScanCache()
reconcile_lock = Lock()
stockout_backoff = StockoutBackoff()


class ReconcileService:
    """Top up missing capacity and retire runners that are safely idle."""

    def __init__(self, github=None, gcloud=None, webhook=None, now=None, cache=None, backoff=None):
        self.github = github or GitHubClient()
        self.gcloud = gcloud or GCloudClient()
        self.webhook = webhook or WebhookService()
        self.now = now or datetime.now(timezone.utc)
        self.cache = cache if cache is not None else repo_scan_cache
        self.backoff = backoff if backoff is not None else stockout_backoff
        self.job_grace = int(os.environ.get('RECONCILE_JOB_GRACE_SECONDS', '120'))
        self.idle_limit = int(os.environ.get('RECONCILE_IDLE_SECONDS', '1200'))
        self.registration_limit = int(os.environ.get('RECONCILE_REGISTRATION_SECONDS', '900'))
        self.assignment_grace = int(os.environ.get('RECONCILE_ASSIGNMENT_GRACE_SECONDS', '300'))
        self.full_scan_interval = int(os.environ.get('RECONCILE_FULL_SCAN_SECONDS', '1800'))
        self.hot_repo_interval = int(os.environ.get('RECONCILE_HOT_REPO_SECONDS', '21600'))

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

    def _inventory(self, scopes, token, summary, vms):
        runners = {}
        supply = defaultdict(int)
        covered = {}
        now_epoch = int(self.now.timestamp())
        for vm in vms:
            scope = self._vm_scope(vm, scopes)
            label = (vm.labels or {}).get('gha-runner')
            if not scope or not label:
                continue
            key = (scope, label)
            entry = label_entry(summary, scope, label)
            if scope not in runners:
                runners[scope] = {item['name']: item for item in self.github.list_runners(scope, token)}
            runner = runners[scope].get(vm.name)
            job_id = (vm.labels or {}).get('gha-job')
            age = (self.now - parse_time(vm.creation_timestamp)).total_seconds()
            recent_job = (job_id and age < self.assignment_grace)
            idle_since = (vm.labels or {}).get('gha-idle-since')
            counted = False
            if runner:
                if runner.get('busy') is not False or runner.get('status') != 'online':
                    if idle_since:
                        self.gcloud.set_runner_idle_since(vm, None)
                    if recent_job:
                        covered[(scope, label, str(job_id))] = 0
                    continue
                if not idle_since:
                    self.gcloud.set_runner_idle_since(vm, now_epoch)
                    counted = True
                elif now_epoch - int(idle_since) < self.idle_limit:
                    counted = True
            else:
                if age < self.registration_limit:
                    counted = True
            if counted:
                supply[key] += 1
                if recent_job:
                    covered[(scope, label, str(job_id))] = 1
                continue
            if self._can_delete(scope, vm.name, token, runner is not None):
                self.gcloud.delete_runner_instance(vm.name, delivery_id='reconcile')
                entry['deleted'] += 1
        return supply, covered

    def run(self):
        started = time.monotonic()
        summary = {'event': 'reconcile', 'labels': {}, 'capacity_stop': False, 'timing_ms': {},
                   'attempted': 0, 'created': 0, 'errors': {}, 'stockout_probe': self.backoff.active}
        if not reconcile_lock.acquire(blocking=False):
            summary['skipped'] = 'already_running'
            log_tick(summary)
            return summary
        try:
            phase = time.monotonic()
            token = self.github.get_installation_access_token()
            summary['timing_ms']['github_auth'] = round((time.monotonic() - phase) * 1000)
            phase = time.monotonic()
            repos = self.github.list_installation_repositories(token)
            summary['timing_ms']['github_repos'] = round((time.monotonic() - phase) * 1000)
            scopes = {scope_for_repo(repo) for repo in repos}
            phase = time.monotonic()
            vms = self.gcloud.list_runner_instances()
            summary['timing_ms']['gce_instances'] = round((time.monotonic() - phase) * 1000)
            scan_repos, full_scan = self.cache.select(
                repos, vms, self.now, self.full_scan_interval, self.hot_repo_interval,
            )
            summary['scanned_repos'] = len(scan_repos)
            summary['full_scan'] = full_scan
            demand = defaultdict(list)
            queued_ids = set()
            phase = time.monotonic()
            try:
                for repo in scan_repos:
                    scope = scope_for_repo(repo)
                    jobs = self.github.list_queued_workflow_jobs(repo['full_name'], token)
                    self.cache.observe(repo['full_name'], jobs, self.now)
                    for job in jobs:
                        queued_ids.add(str(job['id']))
                        age = (self.now - parse_time(job['created_at'])).total_seconds()
                        if age < self.job_grace:
                            continue
                        label = next((label for label in job.get('labels', []) if label.startswith('gcp-')), None)
                        if label:
                            demand[(scope, label)].append((job['created_at'], repo, str(job['id'])))
            finally:
                summary['timing_ms']['github_jobs'] = round((time.monotonic() - phase) * 1000)
            self.cache.complete(self.now, full_scan)

            phase = time.monotonic()
            supply, covered = self._inventory(scopes, token, summary, vms)
            summary['timing_ms']['inventory'] = round((time.monotonic() - phase) * 1000)
            for key, count in supply.items():
                scope, label = key
                summary['labels'][f'{scope[0]}:{scope[1]}/{label}']['supply'] = count
            probe_remaining = 1 if self.backoff.active else None
            for key, jobs in sorted(demand.items()):
                scope, label = key
                entry = label_entry(summary, scope, label)
                remaining = [item for item in jobs if (scope, label, item[2]) not in covered]
                reserved = sum(count for (vm_scope, vm_label, job_id), count in covered.items()
                               if vm_scope == scope and vm_label == label and job_id in queued_ids)
                available = max(0, supply[key] - reserved)
                entry['demand'] = len(remaining)
                entry['supply'] = available
                if len(remaining) != len(jobs):
                    entry['recent_assignments'] = len(jobs) - len(remaining)
                for _, repo, job_id in sorted(remaining, key=lambda item: item[0])[available:]:
                    if probe_remaining == 0:
                        summary['capacity_stop'] = True
                        return summary
                    owner = repo['owner']
                    entry['attempted'] += 1
                    summary['attempted'] += 1
                    if probe_remaining is not None:
                        probe_remaining -= 1
                    try:
                        name = self.webhook._handle_queued_job(
                            label, repo['html_url'], owner['html_url'], repo['full_name'],
                            owner['login'] if scope[0] == 'org' else None,
                            delivery_id='reconcile',
                            job_id=job_id,
                        )
                    except Exception as error:
                        reason = insert_error_reason(error)
                        entry['errors'][reason] = entry['errors'].get(reason, 0) + 1
                        summary['errors'][reason] = summary['errors'].get(reason, 0) + 1
                        if reason == 'stockout':
                            self.backoff.active = True
                        if is_capacity_error(error):
                            summary['capacity_stop'] = True
                            return summary
                        raise
                    if not name:
                        entry['unsupported_template'] = True
                        entry['errors']['unsupported_template'] = entry['errors'].get('unsupported_template', 0) + 1
                        summary['errors']['unsupported_template'] = summary['errors'].get('unsupported_template', 0) + 1
                        break
                    self.backoff.active = False
                    probe_remaining = None
                    entry['created'] += 1
                    summary['created'] += 1
            return summary
        except Exception as error:
            summary['error'] = type(error).__name__
            if hasattr(error, 'endpoint_path'):
                summary['error_status'] = error.status_code
                summary['error_endpoint'] = error.endpoint_path
                summary['error_reason'] = error.reason
            raise
        finally:
            summary['timing_ms']['total'] = round((time.monotonic() - started) * 1000)
            reconcile_lock.release()
            log_tick(summary)

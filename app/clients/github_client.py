"""
GitHub Client for authenticating and interacting with the GitHub API.
"""
import os
import time
import jwt
import requests
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

REQUEST_TIMEOUT = 30  # seconds

logger = logging.getLogger(__name__)


class GitHubReadError(RuntimeError):
    """Safe context for a failed GitHub read; never includes credentials or response bodies."""

    def __init__(self, path, status_code, reason):
        self.endpoint_path = path
        self.status_code = status_code
        self.reason = reason
        super().__init__(f'GitHub read {reason}: status={status_code}, path={path}')


class GitHubClient:
    """Client for authenticated interactions with the GitHub API as a GitHub App."""

    def __init__(self):
        """Initialize GitHubClient with environment configuration."""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_INSTALLATION_ID')
        self.private_key = os.environ.get('GITHUB_PRIVATE_KEY')
        self.private_key_path = os.environ.get('GITHUB_PRIVATE_KEY_PATH')
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')

        if not all([self.app_id, self.installation_id]) or not (self.private_key_path or self.private_key):
            logger.warning("GitHub App configuration missing.")

    def _get_private_key(self):
        """
        Retrieve the GitHub App private key.

        Returns:
            str: The private key content.

        Raises:
            ValueError: If no private key source is configured.
        """
        # Retrun environment variable
        if self.private_key:
            return self.private_key
        # Return file content
        elif self.private_key_path:
            with open(self.private_key_path, 'r') as f:
                return f.read()
        else:
            raise ValueError("No private key source configured.")

    def _generate_jwt(self):
        """Generates a JWT for GitHub App authentication."""
        try:
            private_key = self._get_private_key()

            payload = {
                'iat': int(time.time()),
                'exp': int(time.time()) + (10 * 60),
                'iss': self.app_id
            }

            encoded_jwt = jwt.encode(payload, private_key, algorithm='RS256')
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error generating JWT: {e}")
            raise

    def get_installation_access_token(self):
        """Obtains an installation access token."""
        # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
        jwt_token = self._generate_jwt()
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }
        url = f'https://api.github.com/app/installations/{self.installation_id}/access_tokens'

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        # The installation access token will expire after 1 hour.
        return response.json()['token']

    def get_registration_token(self, org_name=None, repo_name=None, delivery_id=None):
        """Gets a runner registration token."""
        # https://docs.github.com/en/rest/actions/self-hosted-runners
        token = self.get_installation_access_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }
        if org_name:
            # GitHub Docs: https://t.ly/dAyGK
            url = f"https://api.github.com/orgs/{org_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for organization: %s, delivery_id: %s",
                org_name,
                delivery_id,
            )
        elif repo_name:
            # GitHub Docs: https://t.ly/n0w2a
            url = f"https://api.github.com/repos/{repo_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for repository: %s, delivery_id: %s",
                repo_name,
                delivery_id,
            )
        else:
            raise ValueError("Either org_name or repo_name must be provided")

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()['token']

    def _list_pages(self, path, key, token, params=None):
        """Read every page; retry a changing result set before failing closed."""
        for attempt in range(3):
            try:
                return self._list_pages_once(path, key, token, params)
            except GitHubReadError as error:
                if error.reason != 'incomplete_pagination' or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _list_pages_once(self, path, key, token, params):
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        items = []
        page = 1
        while True:
            query = dict(params or {}, per_page=100, page=page)
            try:
                response = requests.get(f'https://api.github.com{path}', headers=headers,
                                        params=query, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
            except requests.RequestException as error:
                status = getattr(getattr(error, 'response', None), 'status_code', None)
                raise GitHubReadError(path, status, 'http_error') from error
            body = response.json()
            batch = body[key] if key else body
            items.extend(batch)
            if len(batch) < 100:
                if body.get('total_count', len(items)) > len(items):
                    raise GitHubReadError(path, response.status_code, 'incomplete_pagination')
                return items
            page += 1

    def list_installation_repositories(self, token):
        return self._list_pages('/installation/repositories', 'repositories', token)

    def list_queued_workflow_jobs(self, repo_name, token):
        """Inspect jobs in every active workflow run, including runs at max-parallel."""
        repo = quote(repo_name, safe='/')
        jobs = {}
        statuses = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
        with ThreadPoolExecutor(max_workers=6) as executor:
            runs_by_status = executor.map(
                lambda status: self._list_pages(f'/repos/{repo}/actions/runs', 'workflow_runs', token,
                                                {'status': status}), statuses,
            )
            run_ids = list(dict.fromkeys(run['id'] for runs in runs_by_status for run in runs))
            job_pages = executor.map(
                lambda run_id: self._list_pages(f'/repos/{repo}/actions/runs/{run_id}/jobs', 'jobs', token),
                run_ids,
            )
            for run_jobs in job_pages:
                for job in run_jobs:
                    if job.get('status') == 'queued':
                        jobs[job['id']] = job
        return list(jobs.values())

    def list_runners(self, scope, token):
        kind, name = scope
        if kind == 'org':
            path = f'/orgs/{quote(name, safe="")}/actions/runners'
        else:
            path = f'/repos/{quote(name, safe="/")}/actions/runners'
        return self._list_pages(path, 'runners', token)

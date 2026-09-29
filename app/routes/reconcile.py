"""Cloud Scheduler-only reconciliation endpoint."""
import logging
import os

from flask import Blueprint, jsonify, request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from app.clients.github_client import GitHubReadError
from app.services.reconcile_service import ReconcileService

logger = logging.getLogger(__name__)
reconcile_bp = Blueprint('reconcile', __name__)


def authorized_scheduler_request():
    audience = os.environ.get('RECONCILE_AUDIENCE')
    expected_email = os.environ.get('RECONCILE_SERVICE_ACCOUNT')
    header = request.headers.get('Authorization', '')
    if not audience or not expected_email or not header.startswith('Bearer '):
        return False
    try:
        claims = id_token.verify_oauth2_token(header[7:], GoogleRequest(), audience=audience)
    except Exception:
        return False
    return (claims.get('email') == expected_email
            and claims.get('email_verified') is True
            and claims.get('aud') == audience)


@reconcile_bp.route('/reconcile', methods=['POST'])
def reconcile():
    if not authorized_scheduler_request():
        return jsonify({'status': 'forbidden'}), 403
    try:
        return jsonify(ReconcileService().run()), 200
    except Exception as error:
        if isinstance(error, GitHubReadError):
            logger.error('Reconciliation failed: %s status=%s endpoint=%s reason=%s',
                         type(error).__name__, error.status_code, error.endpoint_path, error.reason)
        else:
            logger.error('Reconciliation failed: %s', type(error).__name__)
        return jsonify({'status': 'error'}), 500

"""Cloud Scheduler authentication must gate all reconciliation work."""
from unittest.mock import patch


def test_missing_oidc_token_is_forbidden(client):
    assert client.post('/reconcile').status_code == 403


@patch('app.routes.reconcile.ReconcileService')
@patch('app.routes.reconcile.id_token.verify_oauth2_token')
def test_valid_scheduler_identity(mock_verify, mock_service, client, monkeypatch):
    monkeypatch.setenv('RECONCILE_AUDIENCE', 'https://manager.example.run.app')
    monkeypatch.setenv('RECONCILE_SERVICE_ACCOUNT', 'scheduler@example.iam.gserviceaccount.com')
    mock_verify.return_value = {
        'aud': 'https://manager.example.run.app',
        'email': 'scheduler@example.iam.gserviceaccount.com', 'email_verified': True,
    }
    mock_service.return_value.run.return_value = {'event': 'reconcile'}
    assert client.post('/reconcile', headers={'Authorization': 'Bearer test-token'}).status_code == 200
    mock_service.return_value.run.assert_called_once()


@patch('app.routes.reconcile.ReconcileService')
@patch('app.routes.reconcile.id_token.verify_oauth2_token')
def test_wrong_scheduler_identity_is_forbidden(mock_verify, mock_service, client, monkeypatch):
    monkeypatch.setenv('RECONCILE_AUDIENCE', 'https://manager.example.run.app')
    monkeypatch.setenv('RECONCILE_SERVICE_ACCOUNT', 'scheduler@example.iam.gserviceaccount.com')
    mock_verify.return_value = {
        'aud': 'https://manager.example.run.app',
        'email': 'other@example.iam.gserviceaccount.com', 'email_verified': True,
    }
    assert client.post('/reconcile', headers={'Authorization': 'Bearer test-token'}).status_code == 403
    mock_service.assert_not_called()

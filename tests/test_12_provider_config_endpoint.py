import json
import pytest

from oidcmsg.key_jar import build_keyjar

from oidcendpoint.oidc import userinfo
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.token import AccessToken
from oidcendpoint.endpoint_context import EndpointContext

KEYDEFS = [
    {"type": "RSA", "key": '', "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]}
]

KEYJAR = build_keyjar(KEYDEFS)[1]

RESPONSE_TYPES_SUPPORTED = [
    ["code"], ["token"], ["id_token"], ["code", "token"], ["code", "id_token"],
    ["id_token", "token"], ["code", "token", "id_token"], ['none']]

CAPABILITIES = {
    "response_types_supported": [" ".join(x) for x in RESPONSE_TYPES_SUPPORTED],
    "token_endpoint_auth_methods_supported": [
        "client_secret_post", "client_secret_basic",
        "client_secret_jwt", "private_key_jwt"],
    "response_modes_supported": ['query', 'fragment', 'form_post'],
    "subject_types_supported": ["public", "pairwise"],
    "grant_types_supported": [
        "authorization_code", "implicit",
        "urn:ietf:params:oauth:grant-type:jwt-bearer", "refresh_token"],
    "claim_types_supported": ["normal", "aggregated", "distributed"],
    "claims_parameter_supported": True,
    "request_parameter_supported": True,
    "request_uri_parameter_supported": True,
}


class TestEndpoint(object):
    @pytest.fixture(autouse=True)
    def create_endpoint(self):
        conf = {
            "issuer": "https://example.com/",
            "password": "mycket hemligt",
            "token_expires_in": 600,
            "grant_expires_in": 300,
            "refresh_token_expires_in": 86400,
            "verify_ssl": False,
            "capabilities": CAPABILITIES,
            "jwks": {
                'public_path': 'jwks.json',
                'local_path': 'static/jwks.json',
                'private_path': 'own/jwks.json'
            },
            'endpoint': {
                'provider_config': {
                    'path': '.well-known/openid-configuration',
                    'class': ProviderConfiguration,
                    'kwargs': {}
                },
                'registration': {
                    'path': 'registration',
                    'class': Registration,
                    'kwargs': {}
                },
                'authorization': {
                    'path': 'authorization',
                    'class': Authorization,
                    'kwargs': {}
                },
                'token': {
                    'path': 'token',
                    'class': AccessToken,
                    'kwargs': {}
                },
                'userinfo': {
                    'path': 'userinfo',
                    'class': userinfo.UserInfo,
                    'kwargs': {'db_file': 'users.json'}
                }
            },
            'template_dir': 'template'
        }
        self.endpoint_context = EndpointContext(conf, keyjar=KEYJAR)
        self.endpoint = ProviderConfiguration(self.endpoint_context)

    def test_do_response(self):
        args = self.endpoint.process_request()
        msg = self.endpoint.do_response(args['response_args'])
        assert isinstance(msg, dict)
        _msg = json.loads(msg['response'])
        assert _msg
        assert _msg['token_endpoint'] == 'https://example.com/token'
        assert _msg['jwks_uri'] == 'https://example.com/jwks.json'
        assert ('Content-type', 'application/json') in msg['http_headers']
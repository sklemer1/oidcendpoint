import base64

from cryptojwt import as_bytes, as_unicode
from oicmsg.jwt import JWT
from requests import request

from oicmsg.key_jar import build_keyjar, KeyJar

from oicsrv import JWT_BEARER
from oicsrv.client_authn import ClientSecretBasic, ClientSecretPost, \
    ClientSecretJWT, PrivateKeyJWT
from oicsrv.srv_info import SrvInfo
from oicsrv.user_authn.authn_context import INTERNETPROTOCOLPASSWORD

KEYDEFS = [
    {"type": "RSA", "key": '', "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]}
]

KEYJAR = build_keyjar(KEYDEFS)[1]

conf = {
    "base_url": "https://example.com",
    "issuer": "https://example.com/",
    "password": "mycket hemligt",
    "token_expires_in": 600,
    "grant_expires_in": 300,
    "refresh_token_expires_in": 86400,
    "verify_ssl": False,
    "authentication": [{
        'acr': INTERNETPROTOCOLPASSWORD,
        'name': 'NoAuthn',
        'args': {'user': 'diana'}
    }]
}
client_id = 'client_id'
client_secret = 'client_secret'
# Need to add the client_secret as a symmetric key bound to the client_id
KEYJAR.add_symmetric(client_id, client_secret, ['sig'])

srv_info = SrvInfo(conf, keyjar=KEYJAR, httplib=request)
srv_info.cdb[client_id] = {'client_secret': client_secret}


def test_client_secret_basic():
    _token = '{}:{}'.format(client_id, client_secret)
    token = as_unicode(base64.b64encode(as_bytes(_token)))

    authz_token = 'Basic {}'.format(token)

    authn_info = ClientSecretBasic(srv_info).verify({}, authz_token)

    assert authn_info['client_id'] == client_id


def test_client_secret_post():
    request = {'client_id': client_id, 'client_secret': client_secret}

    authn_info = ClientSecretPost(srv_info).verify(request)

    assert authn_info['client_id'] == client_id


def test_client_secret_jwt():
    client_keyjar = KeyJar()
    client_keyjar[conf['issuer']] = KEYJAR.issuer_keys['']
    # The only own key the client has a this point
    client_keyjar.add_symmetric('', client_secret, ['sig'])

    _jwt = JWT(client_keyjar, iss=client_id, sign_alg='HS256')
    _assertion = _jwt.pack({'aud': [conf['issuer']]})

    request = {'client_assertion': _assertion,
               'client_assertion_type': JWT_BEARER}

    authn_info = ClientSecretJWT(srv_info).verify(request)

    assert authn_info['client_id'] == client_id
    assert 'jwt' in authn_info


def test_private_key_jwt():
    # Own dynamic keys
    client_keyjar = build_keyjar(KEYDEFS)[1]
    # The servers keys
    client_keyjar[conf['issuer']] = KEYJAR.issuer_keys['']

    _jwks = client_keyjar.export_jwks()
    srv_info.keyjar.import_jwks(_jwks, client_id)

    _jwt = JWT(client_keyjar, iss=client_id, sign_alg='RS256')
    _assertion = _jwt.pack({'aud': [conf['issuer']]})

    request = {'client_assertion': _assertion,
               'client_assertion_type': JWT_BEARER}

    authn_info = PrivateKeyJWT(srv_info).verify(request)

    assert authn_info['client_id'] == client_id
    assert 'jwt' in authn_info
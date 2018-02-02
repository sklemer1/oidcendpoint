import json
import os
from oicmsg.message import Message
from oicmsg.oic import AuthorizationRequest, OpenIDSchema
from oicmsg.oic import OpenIDRequest
from oicsrv.srv_info import SrvInfo
from oicsrv.user_authn.authn_context import INTERNETPROTOCOLPASSWORD
from oicsrv.user_info import UserInfo
from oicsrv.userinfo import claims_match, by_schema, collect_user_info
from oicsrv.userinfo import update_claims
from requests import request

CLAIMS = {
    "userinfo": {
        "given_name": {"essential": True},
        "nickname": None,
        "email": {"essential": True},
        "email_verified": {"essential": True},
        "picture": None,
        "http://example.info/claims/groups": {'value': 'red'}
    },
    "id_token": {
        "auth_time": {"essential": True},
        "acr": {"values": ["urn:mace:incommon:iap:silver"]}
    }
}

AREQO = AuthorizationRequest(response_type="code", client_id="client1",
                             redirect_uri="http://example.com/authz",
                             scope=["openid", "offlien_access"],
                             state="state000", claims=CLAIMS)

OIDR = OpenIDRequest(response_type="code", client_id="client1",
                     redirect_uri="http://example.com/authz", scope=["openid"],
                     state="state000", claims=CLAIMS)

SESSION_INFO = {
    'oidreq': OIDR.to_json(),
    'authzreq': AREQO.to_json()
}

BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


USERINFO = UserInfo(json.loads(open(full_path('users.json')).read()))


def test_update_claims_oidreq_id_token():
    claims = update_claims(SESSION_INFO, 'oidreq', 'id_token')
    assert set(claims.keys()) == {'auth_time', 'acr'}


def test_update_claims_oidreq_userinfo():
    claims = update_claims(SESSION_INFO, 'oidreq', 'userinfo')
    assert set(claims.keys()) == {'given_name', 'nickname', 'email',
                                  'email_verified', 'picture',
                                  'http://example.info/claims/groups'}


def test_update_claims_authzreq_id_token():
    claims = update_claims(SESSION_INFO, 'oidreq', 'id_token')
    assert set(claims.keys()) == {'auth_time', 'acr'}


def test_update_claims_authzreq_userinfo():
    claims = update_claims(SESSION_INFO, 'oidreq', 'userinfo')
    assert set(claims.keys()) == {'given_name', 'nickname', 'email',
                                  'email_verified', 'picture',
                                  'http://example.info/claims/groups'}


def test_clams_value():
    assert claims_match('red',
                        CLAIMS['userinfo']["http://example.info/claims/groups"])


def test_clams_values():
    assert claims_match("urn:mace:incommon:iap:silver",
                        CLAIMS['id_token']['acr'])


def test_clams_essential():
    assert claims_match(['foobar@example'],
                        CLAIMS['userinfo']['email'])


def test_clams_none():
    assert claims_match(['angle'],
                        CLAIMS['userinfo']['nickname'])


def test_by_schema():
    # There are no requested or optional claims defined for Message
    assert by_schema(Message, sub='John') == {}

    assert by_schema(OpenIDSchema, sub='John', given_name='John', age=34) == {
        'sub': 'John', 'given_name': 'John'}


def test_collect_user_info():
    session = SESSION_INFO.copy()
    session.update({'scope': ['openid', 'profile'], 'sub': 'doe',
                    'uid': 'diana', 'client_id': 'client'})

    srv_info = SrvInfo({'userinfo': USERINFO,
                        'password': "we didn't start the fire",
                        'base_url': 'https://example.com/op',
                        'token_expires_in': 900, 'grant_expires_in': 600,
                        'refresh_token_expires_in': 86400,
                        "authentication": [{
                            'acr': INTERNETPROTOCOLPASSWORD,
                            'name': 'NoAuthn',
                            'args': {'user': 'diana'}}]
                        },
                       httplib=request)

    res = collect_user_info(srv_info, session)

    assert res == {'given_name': 'Diana', 'nickname': 'Dina',
                   'email': 'diana@example.org', 'email_verified': False,
                   'sub': 'doe', 'name': 'Diana Krall', 'family_name': 'Krall'}
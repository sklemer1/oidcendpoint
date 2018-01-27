import copy
import logging
from functools import cmp_to_key
from urllib.parse import urljoin

from jwkest import jwe
from jwkest import jws
from oiccli import rndstr
from oiccli.http import HTTPLib
from oicmsg.key_jar import KeyJar
from oicmsg.oic import SCOPE2CLAIMS

from oicsrv import authz
from oicsrv.exception import ConfigurationError
from oicsrv.sdb import create_session_db
from oicsrv.sso_db import SSODb
from oicsrv.user_authn import user
from oicsrv.user_authn.authn_context import AuthnBroker

logger = logging.getLogger(__name__)

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
    "request_uri_parameter_supported": True
}

SORT_ORDER = {'RS': 0, 'ES': 1, 'HS': 2, 'PS': 3, 'no': 4}


def sort_sign_alg(alg1, alg2):
    if SORT_ORDER[alg1[0:2]] < SORT_ORDER[alg2[0:2]]:
        return -1
    elif SORT_ORDER[alg1[0:2]] > SORT_ORDER[alg2[0:2]]:
        return 1
    else:
        if alg1 < alg2:
            return -1
        elif alg1 > alg2:
            return 1
        else:
            return 0


class SrvInfo(object):
    def __init__(self, conf, httplib=None, keyjar=None, client_db=None,
                 session_db=None):
        self.conf = conf
        self.keyjar = keyjar or KeyJar()

        self.http = httplib or HTTPLib(ca_certs=conf['ca_certs'],
                                       verify_ssl=conf['verify_ssl'],
                                       client_cert=conf['client_cert'],
                                       keyjar=keyjar)

        if session_db:
            self.sdb = session_db
        else:
            self.sdb = create_session_db(
                conf['base_url'], conf['password'], db=None,
                token_expires_in=conf['token_expires_in'],
                grant_expires_in=conf['grant_expires_in'],
                refresh_token_expires_in=conf['refresh_token_expires_in'],
                sso_db=SSODb())

        # client database
        self.cdb = client_db or {}

        try:
            self.seed = bytes(conf['seed'], 'utf-8')
        except KeyError:
            self.seed = bytes(rndstr(16), 'utf-8')

        # Default values, to be changed below depending on configuration
        self.endpoint = {}
        self.base_url = ''
        self.issuer = ''
        self.verify_ssl = True
        self.jwks_uri = None
        self.sso_ttl = 14400  # 4h
        self.cookie_name = 'oicsrc'
        self.symkey = rndstr(24)

        for param in ['verify_ssl', 'base_url', 'issuer', 'jwks_uri',
                      'endpoint', 'sso_ttl', 'cookie_name', 'symkey',
                      'userinfo', 'client_authn']:
            try:
                setattr(self, param, conf[param])
            except KeyError:
                pass

        self.setup = {}

        try:
            _cap = conf['capabilities']
        except KeyError:
            _cap = {}

        try:
            authz_spec = conf['authz']
        except KeyError:
            self.authz = authz.Implicit()
        else:
            if 'args' in authz_spec:
                self.authz = authz.factory(authz_spec['name'],
                                           **authz_spec['args'])
            else:
                self.authz = authz.factory(authz_spec['name'])

        self.authn_broker = AuthnBroker()

        for authn_spec in conf['authentication']:
            try:
                _args = authn_spec['args']
            except KeyError:
                _args = {}
            authn_method = user.factory(authn_spec['name'], **_args)
            authn_method.srv_info = self
            args = {k: authn_spec[k] for k in
                    ['acr', 'level', 'authn_authority'] if k in authn_spec}

            self.authn_broker.add(method=authn_method, **args)

        self.cookie_func = self.authn_broker[0][0].create_cookie
        self.provider_info = self.create_providerinfo(_cap)

        # which signing/encryption algorithms to use in what context
        self.jwx_def = {}

        # special type of logging
        self.events = None

    def package_capabilities(self):
        _provider_info = copy.deepcopy(CAPABILITIES)
        _provider_info["issuer"] = self.issuer
        _provider_info["version"] = "3.0"

        _claims = []
        for _cl in SCOPE2CLAIMS.values():
            _claims.extend(_cl)
        _provider_info["claims_supported"] = list(set(_claims))

        _scopes = list(SCOPE2CLAIMS.keys())
        _scopes.append("openid")
        _provider_info["scopes_supported"] = _scopes

        # Sort order RS, ES, HS, PS
        sign_algs = list(jws.SIGNER_ALGS.keys())
        sign_algs = sorted(sign_algs, key=cmp_to_key(sort_sign_alg))

        for typ in ["userinfo", "id_token", "request_object"]:
            _provider_info["%s_signing_alg_values_supported" % typ] = sign_algs

        # Remove 'none' for token_endpoint_auth_signing_alg_values_supported
        # since it is not allowed
        sign_algs = sign_algs[:]
        sign_algs.remove('none')
        _provider_info[
            "token_endpoint_auth_signing_alg_values_supported"] = sign_algs

        algs = jwe.SUPPORTED["alg"]
        for typ in ["userinfo", "id_token", "request_object"]:
            _provider_info["%s_encryption_alg_values_supported" % typ] = algs

        encs = jwe.SUPPORTED["enc"]
        for typ in ["userinfo", "id_token", "request_object"]:
            _provider_info["%s_encryption_enc_values_supported" % typ] = encs

        # acr_values
        if self.authn_broker:
            acr_values = self.authn_broker.getAcrValuesString()
            if acr_values is not None:
                _provider_info["acr_values_supported"] = acr_values

        return _provider_info

    def create_providerinfo(self, capabilities):
        """
        Dynamically create the provider info response

        :param capabilities:
        :return:
        """

        _pinfo = self.package_capabilities()
        not_supported = {}
        for key, val in capabilities.items():
            try:
                allowed = _pinfo[key]
            except KeyError:
                _pinfo[key] = val
            else:
                if isinstance(allowed, bool):
                    if allowed is False:
                        if val is True:
                            not_supported[key] = True
                    else:
                        _pinfo[key] = val
                elif isinstance(allowed, str):
                    if val != allowed:
                        not_supported[key] = val
                elif isinstance(allowed, list):
                    if isinstance(val, str):
                        sv = {val}
                    else:
                        sv = set(val)

                    sa = set(allowed)

                    if (sv & sa) == sv:
                        _pinfo[key] = list(sv)
                    else:
                        not_supported[key] = list(sv - sa)

        if not_supported:
            _msg = "Server doesn't support the following features: {}".format(
                not_supported)
            logger.error(_msg)
            raise ConfigurationError(_msg)

        if self.jwks_uri and self.keyjar:
            _pinfo["jwks_uri"] = self.jwks_uri

        if not self.base_url.endswith('/'):
            baseurl = self.base_url + '/'
        else:
            baseurl = self.base_url

        for name, path in self.endpoint.items():
            _pinfo['{}_endpoint'.format(name)] = urljoin(baseurl, path)

        return _pinfo
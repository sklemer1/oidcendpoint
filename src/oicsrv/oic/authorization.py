import logging

from urllib.parse import parse_qs
from urllib.parse import splitquery
from urllib.parse import unquote
from urllib.parse import urlparse

from cryptojwt.jwe import JWEException
from cryptojwt.jws import NoSuitableSigningKeys
from oiccli.exception import InvalidRequest
from oicmsg import oic
from oicmsg.exception import ParameterError
from oicmsg.exception import UnSupported
from oicmsg.exception import URIError
from oicmsg.oauth2 import AuthorizationErrorResponse
from oicmsg.oauth2 import ErrorResponse
from oicmsg.oic import AuthorizationResponse

from oicsrv import sanitize
from oicsrv.endpoint import Endpoint
from oicsrv.exception import NoSuchAuthentication
from oicsrv.exception import RedirectURIError
from oicsrv.exception import TamperAllert
from oicsrv.exception import ToOld
from oicsrv.exception import UnknownClient
from oicsrv.id_token import sign_encrypt_id_token
from oicsrv.sdb import AuthnEvent
from oicsrv.user_authn.authn_context import pick_auth
from oicsrv.userinfo import userinfo_in_id_token_claims
from oicsrv.userinfo import collect_user_info
from oicsrv.util import make_headers

logger = logging.getLogger(__name__)

FORM_POST = """<html>
  <head>
    <title>Submit This Form</title>
  </head>
  <body onload="javascript:document.forms[0].submit()">
    <form method="post" action={action}>
        {inputs}
    </form>
  </body>
</html>"""


def inputs(form_args):
    """
    Creates list of input elements
    """
    element = []
    for name, value in form_args.items():
        element.append(
            '<input type="hidden" name="{}" value="{}"/>'.format(name, value))
    return "\n".join(element)


def max_age(request):
    try:
        return request["request"]["max_age"]
    except KeyError:
        try:
            return request["max_age"]
        except KeyError:
            return 0


def re_authenticate(request, authn):
    if "prompt" in request and "login" in request["prompt"]:
        if authn.done(request):
            return True

    return False


def setup_session(srv_info, request, authn_event):
    sid = srv_info.sdb.create_authz_session(authn_event, request)
    srv_info.sdb.do_sub(sid, '')
    return sid


def verify_redirect_uri(srv_info, request):
    """
    MUST NOT contain a fragment
    MAY contain query component

    :return: An error response if the redirect URI is faulty otherwise
        None
    """
    try:
        _redirect_uri = unquote(request["redirect_uri"])

        part = urlparse(_redirect_uri)
        if part.fragment:
            raise URIError("Contains fragment")

        (_base, _query) = splitquery(_redirect_uri)
        if _query:
            _query = parse_qs(_query)

        match = False
        for regbase, rquery in srv_info.cdb[str(request["client_id"])][
            "redirect_uris"]:
            # The URI MUST exactly match one of the Redirection URI
            if _base == regbase:
                # every registered query component must exist in the
                # redirect_uri
                if rquery:
                    for key, vals in rquery.items():
                        assert key in _query
                        for val in vals:
                            assert val in _query[key]
                # and vice versa, every query component in the redirect_uri
                # must be registered
                if _query:
                    if rquery is None:
                        raise ValueError
                    for key, vals in _query.items():
                        assert key in rquery
                        for val in vals:
                            assert val in rquery[key]
                match = True
                break
        if not match:
            raise RedirectURIError("Doesn't match any registered uris")
        # ignore query components that are not registered
        return None
    except Exception:
        logger.error("Faulty redirect_uri: %s" % request["redirect_uri"])
        try:
            _cinfo = srv_info.cdb[str(request["client_id"])]
        except KeyError:
            try:
                cid = request["client_id"]
            except KeyError:
                logger.error('No client id found')
                raise UnknownClient('No client_id provided')
            else:
                logger.info("Unknown client: %s" % cid)
                raise UnknownClient(request["client_id"])
        else:
            logger.info("Registered redirect_uris: %s" % sanitize(_cinfo))
            raise RedirectURIError(
                "Faulty redirect_uri: %s" % request["redirect_uri"])


def get_redirect_uri(srv_info, request):
    """ verify that the redirect URI is reasonable

    :param srv_info:
    :param request: The Authorization request
    :return: Tuple of (redirect_uri, Response instance)
        Response instance is not None of matching redirect_uri failed
    """
    if 'redirect_uri' in request:
        verify_redirect_uri(srv_info, request)
        uri = request["redirect_uri"]
    else:
        raise ParameterError(
            "Missing redirect_uri and more than one or none registered")

    return uri


def authn_args_gather(request, authn_class_ref, cinfo, **kwargs):
    # gather information to be used by the authentication method
    authn_args = {"authn_class_ref": authn_class_ref,
                  "query": request.to_urlencoded()}

    if "req_user" in kwargs:
        authn_args["as_user"] = kwargs["req_user"],

    for attr in ["policy_uri", "logo_uri", "tos_uri"]:
        try:
            authn_args[attr] = cinfo[attr]
        except KeyError:
            pass

    for attr in ["ui_locales", "acr_values"]:
        try:
            authn_args[attr] = request[attr]
        except KeyError:
            pass

    return authn_args


def create_authn_response(srv_info, request, sid):
    # create the response
    aresp = AuthorizationResponse()
    try:
        aresp["state"] = request["state"]
    except KeyError:
        pass

    if "response_type" in request and request["response_type"] == ["none"]:
        fragment_enc = False
    else:
        _sinfo = srv_info.sdb[sid]

        try:
            aresp["scope"] = request["scope"]
        except KeyError:
            pass

        rtype = set(request["response_type"][:])
        handled_response_type = []
        if len(rtype) == 1 and "code" in rtype:
            fragment_enc = False
        else:
            fragment_enc = True

        if "code" in request["response_type"]:
            _code = aresp["code"] = srv_info.sdb[sid]["code"]
            handled_response_type.append("code")
        else:
            srv_info.sdb.update(sid, code=None)
            _code = None

        if "token" in rtype:
            _dic = srv_info.sdb.upgrade_to_token(issue_refresh=False,
                                                 key=sid)

            logger.debug("_dic: %s" % sanitize(_dic))
            for key, val in _dic.items():
                if key in aresp.parameters() and val is not None:
                    aresp[key] = val

            handled_response_type.append("token")

        try:
            _access_token = aresp["access_token"]
        except KeyError:
            _access_token = None

        if "id_token" in request["response_type"]:
            user_info = userinfo_in_id_token_claims(srv_info, _sinfo)
            if request["response_type"] == ["id_token"]:
                #  scopes should be returned here
                info = collect_user_info(srv_info, _sinfo)
                if user_info is None:
                    user_info = info
                else:
                    user_info.update(info)

            #client_info = srv_info.cdb[str(request["client_id"])]

            hargs = {}
            if {'code', 'id_token', 'token'}.issubset(rtype):
                hargs = {"code": _code, "access_token": _access_token}
            elif {'code', 'id_token'}.issubset(rtype):
                hargs = {"code": _code}
            elif {'id_token', 'token'}.issubset(rtype):
                hargs = {"access_token": _access_token}

            # or 'code id_token'
            try:
                id_token = sign_encrypt_id_token(srv_info, _sinfo,
                                                 str(request["client_id"]),
                                                 user_info=user_info,
                                                 sign=True, **hargs)
            except (JWEException, NoSuitableSigningKeys) as err:
                logger.warning(str(err))
                return AuthorizationErrorResponse(
                    error="invalid_request",
                    error_description="Could not sign/encrypt id_token")

            aresp["id_token"] = id_token
            _sinfo["id_token"] = id_token
            handled_response_type.append("id_token")

        not_handled = rtype.difference(handled_response_type)
        if not_handled:
            raise UnSupported("unsupported_response_type", list(not_handled))

    return {'response_args': aresp, 'fragment_enc': fragment_enc}


class Authorization(Endpoint):
    request_cls = oic.AuthorizationRequest
    response_cls = oic.AuthorizationResponse
    request_format = 'urlencoded'
    response_format = 'urlencoded'
    response_placement = 'url'

    def __init__(self, keyjar, **kwargs):
        Endpoint.__init__(self, keyjar, **kwargs)
        # self.pre_construct.append(self._pre_construct)
        self.post_parse_request.append(self._post_parse_request)

    def filter_request(self, srv_info, req):
        return req

    def verify_response_type(self, request, cinfo):
        # Checking response types
        try:
            _registered = [set(rt.split(' ')) for rt in cinfo['response_types']]
        except KeyError:
            # If no response_type is registered by the client then we'll
            # code which it the default according to the OIDC spec.
            _registered = [{'code'}]

        # Is the asked for response_type among those that are permitted
        return set(request["response_type"]) not in _registered

    def _post_parse_request(self, srv_info, request, client_id, **kwargs):
        """

        :param srv_info:
        :param request:
        :param client_id:
        :param kwargs:
        :return:
        """
        if not request:
            logger.debug("No AuthzRequest")
            return AuthorizationErrorResponse(
                error="invalid_request",
                error_description="Can not parse AuthzRequest")

        request = self.filter_request(srv_info, request)

        try:
            _cinfo = srv_info.cdb[client_id]
        except KeyError:
            logger.error(
                'Client ID ({}) not in client database'.format(
                    request['client_id']))
            return AuthorizationErrorResponse(
                error='unauthorized_client', error_description='unknown client')

        # Is the asked for response_type among those that are permitted
        if not self.verify_response_type(request, _cinfo):
            return AuthorizationErrorResponse(
                error="invalid_request",
                error_description="Trying to use unregistered response_typ")

        # Get the redirect URI
        try:
            redirect_uri = get_redirect_uri(srv_info, request)
        except (RedirectURIError, ParameterError, UnknownClient) as err:
            return AuthorizationErrorResponse(
                error="invalid_request",
                error_description="{}:{}".format(err.__class__.__name__, err))
        else:
            request['redirect_uri'] = redirect_uri

        return request

    @staticmethod
    def _acr_claims(request):
        try:
            acrdef = request["claims"]["id_token"]["acr"]
        except KeyError:
            return None
        else:
            if isinstance(acrdef, dict):
                try:
                    return [acrdef["value"]]
                except KeyError:
                    try:
                        return acrdef["values"]
                    except KeyError:
                        pass

        return None

    def pick_authn_method(self, srv_info, request, redirect_uri):
        """
        
        :param srv_info: 
        :param request: 
        :param redirect_uri: 
        :return: 
        """
        acrs = self._acr_claims(request)
        if acrs:
            # If acr claims are present the picked acr value MUST match
            # one of the given
            tup = (None, None)
            for acr in acrs:
                res = srv_info.authn_broker.pick(acr, "exact")
                logger.debug("Picked AuthN broker for ACR %s: %s" % (
                    str(acr), str(res)))
                if res:  # Return the best guess by pick.
                    tup = res[0]
                    break
            authn, authn_class_ref = tup
        else:
            authn, authn_class_ref = pick_auth(srv_info, request)
            if not authn:
                authn, authn_class_ref = pick_auth(srv_info, request, "better")
                if not authn:
                    authn, authn_class_ref = pick_auth(srv_info, request, "any")

        if authn is None:
            return AuthorizationErrorResponse(error="access_denied",
                                              redirect_uri=redirect_uri,
                                              return_type=request["response_type"])
        else:
            logger.info('Authentication class: {}, acr: {}'.format(
                authn.__class__.__name__, authn_class_ref))

        return authn, authn_class_ref

    def setup_auth(self, srv_info, request, redirect_uri, cinfo, cookie, **kwargs):
        """

        :param srv_info:
        :param request:
        :param redirect_uri:
        :param cinfo:
        :param cookie:
        :param kwargs:
        :return:
        """

        authn, authn_class_ref = self.pick_authn_method(srv_info, request,
                                                        redirect_uri)

        try:
            try:
                _auth_info = kwargs["authn"]
            except KeyError:
                _auth_info = ""

            if "upm_answer" in request and request["upm_answer"] == "true":
                _max_age = 0
            else:
                _max_age = max_age(request)

            identity, _ts = authn.authenticated_as(
                cookie, authorization=_auth_info, max_age=_max_age)
        except (NoSuchAuthentication, TamperAllert):
            identity = None
            _ts = 0
        except ToOld:
            logger.info("Too old authentication")
            identity = None
            _ts = 0
        else:
            logger.info("No active authentication")

        authn_args = authn_args_gather(request, authn_class_ref, cinfo,
                                       **kwargs)

        # To authenticate or Not
        if identity is None:  # No!
            if "prompt" in request and "none" in request["prompt"]:
                # Need to authenticate but not allowed
                return AuthorizationErrorResponse(
                    error="login_required", redirect_uri=redirect_uri,
                    return_type=request["response_type"])
            else:
                return {'function': authn, 'args': authn_args}
        else:
            if re_authenticate(request, authn):
                # demand re-authentication
                return {'function': authn, 'args': authn_args}
            else:
                # I get back a dictionary
                user = identity["uid"]
                if "req_user" in kwargs:
                    sids = srv_info.sdb.get_sids_by_sub(kwargs["req_user"])
                    if sids and user != srv_info.sdb.get_authentication_event(
                            sids[-1]).uid:
                        logger.debug("Wanted to be someone else!")
                        if "prompt" in request and "none" in request["prompt"]:
                            # Need to authenticate but not allowed
                            return AuthorizationErrorResponse(
                                error="login_required",
                                redirect_uri=redirect_uri)
                        else:
                            return {'function': authn, 'args': authn_args}

        authn_event = AuthnEvent(identity["uid"], identity.get('salt', ''),
                                 authn_info=authn_class_ref,
                                 time_stamp=_ts)

        return {"authn_event": authn_event, "identity": identity, "user": user}

    def aresp_check(self, aresp, request):
        return ""

    def response_mode(self, request, **kwargs):
        resp_mode = request["response_mode"]
        if resp_mode == "form_post":
            msg = FORM_POST.format(
                inputs=inputs(kwargs["response_args"].to_dict()),
                action=kwargs["redirect_uri"])
            kwargs['response_args'] = msg
            return kwargs
        elif resp_mode == 'fragment' and not kwargs['fragment_enc']:
            # Can't be done
            raise InvalidRequest("wrong response_mode")
        elif resp_mode == 'query' and kwargs['fragment_enc']:
            # Can't be done
            raise InvalidRequest("wrong response_mode")
        else:
            raise InvalidRequest("Unknown response_mode")

    def post_authn(self, srv_info, user, request, sid, **kwargs):
        """
        Things that are donw after a successful authentication.

        :param srv_info:
        :param user:
        :param request:
        :param sid:
        :param kwargs:
        :return:
        """

        # Do the authorization
        try:
            permission = srv_info.authz(user, client_id=request['client_id'])
            srv_info.sdb.update(sid, permission=permission)
        except Exception:
            raise

        logger.debug("response type: %s" % request["response_type"])

        if srv_info.sdb.is_session_revoked(sid):
            return AuthorizationErrorResponse(
                error="access_denied", error_description="Session is revoked")

        try:
            response_info = create_authn_response(srv_info, request, sid)
        except UnSupported as err:
            return AuthorizationErrorResponse(
                error='unsupported_response_type',
                error_description='{}'.format(err.args))

        if isinstance(response_info, AuthorizationErrorResponse):
            return response_info

        try:
            redirect_uri = get_redirect_uri(srv_info, request)
        except (RedirectURIError, ParameterError) as err:
            return AuthorizationErrorResponse(
                error='invalid_request', error_description="{}".format(err))
        else:
            response_info['return_uri'] = redirect_uri

        # Must not use HTTP unless implicit grant type and native application

        info = self.aresp_check(response_info['response_args'], request)
        if isinstance(info, ErrorResponse):
            return info

        headers = make_headers(srv_info, user, **kwargs)

        # Now about the response_mode. Should not be set if it's obvious
        # from the response_type. Knows about 'query', 'fragment' and
        # 'form_post'.

        if "response_mode" in request:
            try:
                response_info = self.response_mode(request, **response_info)
            except InvalidRequest as err:
                return AuthorizationErrorResponse(
                    error="invalid_request", error_description=str(err))

        response_info['http_headers'] = headers

        return response_info

    def authz_part2(self, srv_info, user, request, sid, **kwargs):
        """
        After the authentication this is where you should end up

        ;param srv_info:
        :param user:
        :param request: The Authorization Request
        :param sid: Session key
        :param kwargs: possible other parameters
        :return: A redirect to the redirect_uri of the client
        """
        resp_info = self.post_authn(srv_info, user, request, sid, **kwargs)
        if isinstance(resp_info, ErrorResponse):
            return resp_info

        # Mix-Up mitigation
        resp_info['response_args']['iss'] = srv_info.issuer
        resp_info['response_args']['client_id'] = request['client_id']

        return resp_info

    def process_request(self, srv_info, request=None, **kwargs):
        """ The AuthorizationRequest endpoint

        :param srv_info: Server info
        :param request: The client request as a dictionary
        :return: res
        """

        _cid = request["client_id"]
        cinfo = srv_info.cdb[_cid]
        try:
            cookie = kwargs['cookie']
        except KeyError:
            cookie = ''

        info = self.setup_auth(srv_info, request, request["redirect_uri"],
                               cinfo, cookie, **kwargs)

        if isinstance(info, ErrorResponse):
            return info

        try:
            # Run the authentication function
            return info['function'](**info['args'])
        except KeyError:  # already authenticated
            logger.debug("- authenticated -")
            logger.debug("AREQ keys: %s" % request.keys())

            sid = setup_session(srv_info, request, info["authn_event"])

            res = self.authz_part2(srv_info, info["user"], request, sid,
                                   cookie=cookie)

            return res

from oidcmsg import oidc
from oidcmsg.oidc import JRD
from oidcmsg.oidc import Link

from oidcendpoint.endpoint import Endpoint

OIC_ISSUER = "http://openid.net/specs/connect/1.0/issuer"

class Discovery(Endpoint):
    request_cls = oidc.DiscoveryRequest
    response_cls = JRD
    request_format = 'urlencoded'
    response_format = 'json'

    def do_response(self, srv_info, response_args=None, request=None, **kwargs):
        """
        **Placeholder for the time being**

        :param srv_info: :py:class:`oicsrv.srv_info.SrvInfo` instance
        :param kwargs: request arguments
        :return:
        """

        links = [Link(href=h, rel=OIC_ISSUER)for h in kwargs['hrefs']]

        _response = JRD(subject=kwargs['subject'], links=links)

        info = {
            'response': _response.to_json(),
            'http_headers': [('Content-type', 'application/json')]
        }

        return info

    def process_request(self, srv_info, request=None):
        return {'subject':request['resource'], 'hrefs':[srv_info.issuer]}
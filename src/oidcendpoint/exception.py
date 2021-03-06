class OidcEndpointError(Exception):
    pass


class InvalidRedirectURIError(OidcEndpointError):
    pass


class InvalidSectorIdentifier(OidcEndpointError):
    pass


class ConfigurationError(OidcEndpointError):
    pass


class NoSuchAuthentication(OidcEndpointError):
    pass


class TamperAllert(OidcEndpointError):
    pass


class ToOld(OidcEndpointError):
    pass


class FailedAuthentication(OidcEndpointError):
    pass


class InstantiationError(OidcEndpointError):
    pass


class ImproperlyConfigured(OidcEndpointError):
    pass


class NotForMe(OidcEndpointError):
    pass


class UnknownAssertionType(OidcEndpointError):
    pass


class RedirectURIError(OidcEndpointError):
    pass


class UnknownClient(OidcEndpointError):
    pass


class UnAuthorizedClient(OidcEndpointError):
    pass


class InvalidCookieSign(Exception):
    pass

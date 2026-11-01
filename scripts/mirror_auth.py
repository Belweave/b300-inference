"""Mirror-only credentials with safe representations and request errors."""
import re

from redact import SecretValue

HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HEADER_VALUE = re.compile(r'[\t\x20-\x7e\x80-\xff]+')


class MirrorAuth:
    __slots__ = ('name', '_value')

    def __init__(self, name, value):
        self.name = name
        self._value = SecretValue(value)

    def __repr__(self):
        return 'MirrorAuth([REDACTED])'

    def headers(self):
        return {self.name: self._value}


def parse_mirror_auth(line):
    if line is None or line == '':
        return None
    name, separator, value = line.partition(':')
    value = value.strip(' \t')
    if not separator or not HEADER_NAME.fullmatch(name) or not HEADER_VALUE.fullmatch(value):
        raise ValueError('Invalid MIRROR_AUTH_HEADER: expected one Name: value header with a nonempty value and no CR/LF or invalid control characters')
    return MirrorAuth(name, value)


class MirrorRequestError(RuntimeError):
    """Does not retain request headers, URLs, response bodies or chained errors."""


async def mirror_request(session, url, auth, consume, **options):
    if auth is not None:
        options.update(headers=auth.headers(), allow_redirects=False)
    failed = False
    try:
        async with session.get(url, **options) as response:
            if auth is not None and 300 <= response.status < 400:
                raise MirrorRequestError('Authenticated mirror redirects are not allowed')
            response.raise_for_status()
            return await consume(response)
    except Exception:
        # aiohttp errors can retain request_info.headers; never expose that
        # object through a traceback, repr, cause or context of our public error.
        failed = True
    if failed:
        raise MirrorRequestError('Mirror request or verification failed; check mirror access, redirects and manifest') from None

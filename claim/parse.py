"""Provider-specific parser hook. Deliberately does not guess SSH credentials."""
import sys


def parse_reply(reply):
    # TODO: implement only after Preetham supplies a sanitized real portal reply.
    # Return host, port, user, key={env_ref: NAME} OR {path: /path/to/key},
    # expires_at (UTC epoch seconds). Never accept/return inline private-key material.
    raise NotImplementedError('claim parser not configured: sanitized portal reply needed')


if __name__ == '__main__':
    try:
        parse_reply(sys.stdin.read())
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(78)

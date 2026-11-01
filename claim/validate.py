"""Validate a normalized claim and resolve a key path reference in memory."""
import json
import os
import re
import sys
import time


def validate(data):
    if set(data) != {'host','port','user','key','expires_at'}:
        raise ValueError('Claim fields must be host, port, user, key, expires_at')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', data['host']):
        raise ValueError('host must be a DNS name or IPv4 address')
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', data['user']):
        raise ValueError('Invalid SSH user')
    if type(data['port']) is not int or not 1<=data['port']<=65535:
        raise ValueError('Invalid port')
    if type(data['expires_at']) is not int or data['expires_at']<=time.time()+660:
        raise ValueError('expires_at must be epoch seconds with >11 minutes remaining')
    key = data['key']
    if set(key) == {'env_ref'} and re.fullmatch(r'[A-Z_][A-Z0-9_]*', key['env_ref']):
        path = os.environ[key['env_ref']]
    elif set(key) == {'path'}:
        path = key['path']
    else:
        raise ValueError('key must contain only path or env_ref, never inline key data')
    if not path.startswith('/') or '\n' in path or not os.path.isfile(path):
        raise ValueError('Key reference must resolve to an existing absolute key path')
    return data['host'], str(data['port']), data['user'], path, str(data['expires_at'])


if __name__ == '__main__':
    for value in validate(json.load(open(sys.argv[1]))):
        print(value)

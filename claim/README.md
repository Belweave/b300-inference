# Claim adapter

The grid portal is an AI chat. No portal requests or purchases are implemented.
`parse.py` is an intentional stub until a sanitized real reply is available.
`reply.fixture.txt` records the missing fixture, not a guessed portal grammar.

The parser must emit JSON with exactly:

```json
{"host":"node.example.invalid","port":22,"user":"ubuntu","key":{"env_ref":"NODE_SSH_KEY_PATH"},"expires_at":0}
```

`expires_at` must be actual UTC epoch seconds; zero above is intentionally invalid.
`key` may instead be `{"path":"/absolute/path/to/existing/key"}`. It must never
contain key bytes. A Keychain integration should load an existing identity into
ssh-agent or resolve a reference, without writing a new secret file. This version
uses an existing key path. IPv6 is not yet supported by the claim transport.

The watcher calls `provider_claim` after availability/start hooks, validates its
output, then runs the same bootstrap for every claim. `bootstrap.sh --claim PATH`
is the Mac-side entry point for an already normalized claim. Node-side bootstrap
has no account-specific logic. Do not paste raw portal replies into the repository.

The watcher derives a stable window ID from host/port/user/expires_at (SHA256),
excluding the key path, so retries and relay calls refer to the same allocation.

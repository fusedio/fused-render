"""Reaching the phone listener (lan.py) from anywhere: a blind TLS relay.

See docs/REMOTE_ACCESS.md for the design. In one paragraph: the computer
dials OUT to a relay and keeps one control connection open; a phone reaches
``https://<host-id>.<relay-domain>/``; the relay reads only the SNI of the
phone's TLS ClientHello, asks the computer for a fresh data connection, and
splices the two byte streams together. The TLS session runs end to end
between the phone and the computer's own listener — the SAME self-signed
certificate the phone pinned when it scanned the pairing QR — so the relay
carries ciphertext it cannot read, alter or replay, and cannot impersonate
the computer. ``<host-id>`` is derived from the computer's Ed25519 key, so
only the key holder can claim it on the relay.

- ``protocol`` — stdlib-only pieces both ends share (SNI parsing, framing,
  host ids, the signed challenge).
- ``relay``    — the relay server (``python -m fused_render.tunnel.relay``).
- ``agent``    — the computer's side: the outbound connection and the splice
  into the phone listener.
"""

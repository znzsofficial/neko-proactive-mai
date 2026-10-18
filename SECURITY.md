# Security and privacy

Do not publish credentials, cookies, QR URLs, private chat, live outbox files or
production configuration in issues. Use synthetic data and redact identifiers.

Neko Proactive Mai schedules an intent for MaiSaKa Planner; it does not directly
generate or send ordinary proactive text. Group/friend allowlists, time windows,
probability, minimum interval and isolation time remain separate safeguards.

The optional Neko Life bridge uses a dedicated `neko.proactive` HMAC capability
key, audience, timestamp and persistent nonce checks. The host does not forward
a trusted caller identity to the receiving API, so body-supplied caller IDs are
not authorization. A capability key is not OS isolation from same-account code.

Life submission is not delivery. Only an exact task/session success receipt is
marked `sent`; ordinary QQ anchors, silence, disconnects and ambiguous RPC
results stay `unknown` and are never automatically retransmitted.

-- Spec 080 §7.2: one row per Graph account; ciphertext only.
CREATE TABLE mcp_hub.provider_tokens (
    account_id             text        PRIMARY KEY,
    provider               text        NOT NULL CHECK (provider IN ('microsoft', 'microsoft-org')),
    key_id                 text        NOT NULL,
    nonce                  bytea       NOT NULL CHECK (octet_length(nonce) = 12),
    refresh_token_ct       bytea       NOT NULL,
    granted_scopes         text        NOT NULL,
    obtained_at            timestamptz NOT NULL,
    rotated_at             timestamptz NOT NULL,
    last_invalid_grant_at  timestamptz,
    version                bigint      NOT NULL DEFAULT 1,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

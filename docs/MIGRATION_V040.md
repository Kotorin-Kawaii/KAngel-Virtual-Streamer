# Migration Guide: v0.3.0 → v0.4.0

v0.4.0 adds Viewer Impression and Sponsor Fund Transparency while preserving the existing
authentication, long-term memory, Sponsor Thanks Wall, danmaku, SC, and WebSocket contracts.

## Upgrade

1. Back up the SQLite database and `.env` file.
2. Update the checkout to `v0.4.0`.
3. Run `uv sync --locked` (or reinstall `requirements.txt`).
4. Review the new settings below. They are disabled by default.
5. Restart the single application process and check `GET /status`.

**No manual database migration is required.** Startup creates the new SQLite tables and indexes
idempotently. Existing accounts, long-term memory, Sponsor Thanks Wall records, and API clients
remain compatible.

## Viewer Impression

Viewer Impression is an authenticated, asynchronous side path for registered users who have
enabled long-term memory. It does not run through the PersonaEngine reply chain, write generated
text back into memory, or change Relationship, Persona, or Stream state.

It remains unavailable until both the feature and a dedicated model are configured:

```dotenv
VIEWER_IMPRESSION__ENABLED=True
AI__VIEWER_MEMORY_ARCHAEOLOGIST_MODEL=replace-with-a-dedicated-model
AI__VIEWER_IMPRESSION_SYNTHESIZER_MODEL=replace-with-a-dedicated-model
AI__VIEWER_IMPRESSION_MODEL=replace-with-a-dedicated-model
AI__VIEWER_IMPRESSION_CRITIC_MODEL=replace-with-a-dedicated-model
```

For multi-provider configurations, each eligible provider must explicitly map the role:

```json
{
  "models": {
    "default": "reply-model",
    "viewer_memory_archaeologist": "dedicated-archaeologist-model",
    "viewer_impression_synthesizer": "dedicated-synthesizer-model",
    "viewer_impression": "dedicated-impression-model",
    "viewer_impression_critic": "dedicated-critic-model"
  }
}
```

All four Deep Reflection v2 roles are explicit and never fall back to the ordinary reply model.
Worker lifecycle settings require a process restart. The default successful-generation cooldown is seven days;
failed or retryable attempts do not consume that cooldown, and a failed replacement leaves the
previous letter intact.

Memory export includes the current letter. Disabling/clearing account long-term memory cancels
active impression tasks and deletes the stored letter under the same privacy boundary.

## Sponsor Fund Transparency

Sponsor Thanks Wall behavior and its nickname-only privacy contract are unchanged. Fund
transparency is controlled independently:

```dotenv
SPONSOR__TRANSPARENCY_ENABLED=True
SPONSOR__FINANCE_SYNC_ENABLED=True
SPONSOR__AFDIAN_USER_ID=
SPONSOR__AFDIAN_TOKEN=
```

`SPONSOR__TRANSPARENCY_ENABLED` exposes the aggregate public endpoint. Finance Sync remains OFF
unless explicitly enabled and requires Afdian credentials. Income is derived only from successful
`query-order` records; the admin API cannot set an arbitrary income total. Maintainers may create,
edit, or void expense entries. Voided entries no longer contribute to public totals.

The public response never includes order IDs, platform user IDs, nicknames paired with amounts,
order messages, payment details, or credentials. Sponsorship grants no application privileges.

An optional live API smoke test exists but never runs automatically:

```bash
uv run python scripts/smoke_afdian_orders.py --allow-live
```

Without `--allow-live`, the script refuses network access. It does not write SQLite and does not
print credentials, identities, order IDs, amounts, timestamps, or response bodies.

## New SQLite State

Startup may create the following feature tables and their indexes:

- `account_viewer_impressions`
- `account_viewer_impression_tasks`
- `sponsor_orders`
- `sponsor_finance_sync_state`
- `sponsor_fund_entries`

The schema changes are additive. Continue running one Python process against each SQLite data
directory; multi-process writers are not supported.

# K93 Advanced Notification System
# For Home Assistant
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/k93_ans/brand/dark_logo.png">
  <img src="custom_components/k93_ans/brand/logo.png" alt="K93 ANS" height="60">
</picture>

A Home Assistant custom integration that centralizes notification handling: one entry point fans a
notification out to configurable recipients and channels, optionally requires acknowledgement via
the built-in persistent notification system, and keeps a JSON-backed history that a companion
Lovelace card can display.

> The Lovelace card lives in a separate repository, **k93-ans-card**, and is installed
> independently (its own HACS "Plugin" listing, or manually). This repo is the backend integration
> only.

## How it works

```
automation/script
      |
      v
service: k93_ans.send_notification  ──fires──>  event: k93_ans_notification
                                                        |
                                                        v
                                          event listener (custom_components/k93_ans/__init__.py)
                                                        |
                                                        v
                                            dispatch.py: resolve channel, filter
                                            recipients by importance/channel, call
                                            notify.* for each match, create a
                                            persistent_notification if required
                                                        |
                                                        v
                                          store.py: append record to history (.storage/)
                                                        |
                                                        v
                                     dispatcher signal ──> websocket_api.py ──> Lovelace card
```

Acknowledgement is bidirectional and converges on the same code path (`dispatch.async_acknowledge`):
tapping the "Acknowledge" action button on a phone notification (`mobile_app_notification_action`
event) and clicking "Acknowledge" on the card or calling `k93_ans.acknowledge` all update the same
stored record, dismiss the persistent_notification, clear the pushed notification on every
companion-app recipient that received it, and push the change to every open card via the websocket
subscription — acknowledging from any one place clears it everywhere.

## Services

### `k93_ans.send_notification`

The single ingestion point. Validates input, builds a notification record, and fires the
`k93_ans_notification` event (the service itself does not dispatch or persist — that happens in the
event listener, so other automations could also trigger the same handling by firing the event
directly).

| Field | Description |
|---|---|
| `title`, `message` | Required. |
| `icon` | An `mdi:` icon (small glyph). If not `mdi:` and `image` isn't set, used as the picture too — kept for backward compatibility; prefer `image` for pictures. |
| `image` | A picture: an image URL or an HA-relative path (`/local/...`, `/api/camera_proxy/...`). Sent as the companion-app picture, embedded as markdown in the persistent notification's message, and shown as a preview in the Lovelace card. |
| `channel` | Channel key (see [Channels](#channels)). Defaults to `"default"`. |
| `importance` | `low` \| `normal` \| `high` \| `critical`. Defaults to `normal`. |
| `actions` | List of `{action, title}` action buttons (max 3). |
| `persistent` | If true, requires acknowledgement via the built-in persistent notification system. Independent of `channel` — there's no dedicated "persistent" channel, since this toggle already covers it. |
| `data` | Arbitrary extra fields passed through to the `notify.*` target; caller-supplied values win over anything K93 ANS builds. |
| `target_recipients` | Restrict delivery to these recipient IDs only; omit to consider all enabled recipients. |
| `home_only` | If true, only deliver to recipients whose assigned [person entity](#recipients) is currently `home`. Recipients with no person assigned are unaffected either way. Defaults to `false`. |
| `live_id` | Turns this into a [live notification](#live-notifications): repeated calls with the same `live_id` update the same notification in place instead of creating a new one. |
| `source` | Free-text label for what triggered the notification. |

If `persistent` ends up true and none of the supplied `actions` already acknowledges itself, an
`Acknowledge` action (`K93_ACK__<notification_id>`) is auto-appended.

### `k93_ans.acknowledge`

`{notification_id}` — marks a notification acknowledged, dismisses its persistent_notification if
it has one, and clears the pushed notification (by `tag`) on every companion-app recipient it was
actually delivered to, via a `notify.*` call with `message: "clear_notification"`. Callable from
automations/scripts, and used internally by the card and by the `mobile_app_notification_action`
listener.

### `k93_ans.end_live_notification`

`{live_id}` — ends a [live notification](#live-notifications): looks up the active notification for
that `live_id` and runs it through the exact same routine as `k93_ans.acknowledge` (dismiss
persistent_notification, clear the phone push, mark acknowledged) with `acknowledged_via:
"live_ended"` instead of `"service"`, so it's distinguishable in history from a real user
acknowledgement. A no-op (not an error) if no active notification exists for that `live_id`.

### `k93_ans.delete_notification`

`{notification_id}` — permanently deletes one notification from history. If it's still
outstanding, its persistent_notification is dismissed and any pushed companion-app notification is
cleared first (the same cleanup `acknowledge` does), so deleting never leaves an orphaned bell entry
or phone notification with no history record behind it.

### `k93_ans.clear_history`

`{include_unacknowledged}` (default `false`) — deletes many notifications at once. By default only
notifications eligible for the History section (`store.async_history_ids`: not `requires_ack`, or
already `acknowledged`) are deleted, so a still-outstanding notification is never silently wiped
out along with the rest; set `include_unacknowledged: true` to delete everything regardless of
pending state.

## Live notifications

For something like a vacuum that should show one continuously-updating notification while it's
running — live stats/a map snapshot as the `message`/`icon` — rather than a new notification every
time it publishes an update:

1. Every update, call `k93_ans.send_notification` with the **same `live_id`** and fresh
   `title`/`message`/`icon`/`data`. `services._build_record` looks up the existing active record
   for that `live_id` (`store.async_get_by_live_id`) and, if found, reuses its `id` and original
   `created` timestamp instead of minting a new notification — only `updated_at` changes.
2. Because the underlying `id` never changes, everything downstream that keys off it updates in
   place automatically, with **no changes needed in `dispatch.py`**: the companion-app payload's
   `tag` (`_build_mobile_app_payload`) stays the same, so the phone replaces the existing push
   rather than stacking a new one; `persistent_notification.async_create` is called with the same
   `notification_id`, which HA already treats as "update this notification" rather than "create
   another one"; and `NotificationStore.async_add` was changed to upsert by `id` (update in place
   if found, insert if not) instead of always inserting, so history gets one row that refreshes
   rather than one row per update. The card's own `_onUpdate` already upserts by `id` in its local
   array for the same reason acknowledgements from other clients show up live, so it needed no
   changes either.
3. When the live session ends, call `k93_ans.end_live_notification` with that `live_id` to dismiss
   and clear it everywhere (see above).

Example automation for a vacuum with live cleaning stats and a live map snapshot, using a
`time_pattern` trigger while cleaning and a state trigger to end it:

```yaml
alias: Vacuum live notification
triggers:
  - trigger: time_pattern
    seconds: "/15"
  - trigger: state
    entity_id: vacuum.kitchen
    to: ["docked", "idle", "returning"]
    id: ended
conditions: []
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: ended
        sequence:
          - action: k93_ans.end_live_notification
            data:
              live_id: vacuum_kitchen
    default:
      - condition: state
        entity_id: vacuum.kitchen
        state: cleaning
      - action: k93_ans.send_notification
        data:
          live_id: vacuum_kitchen
          channel: event
          title: Vacuuming kitchen
          message: >-
            {{ state_attr('vacuum.kitchen', 'battery_level') }}% battery ·
            {{ state_attr('vacuum.kitchen', 'cleaned_area') }} m² cleaned
          image: "{{ state_attr('vacuum.kitchen', 'map_image_url') }}"
mode: single
```

(`map_image_url`/`cleaned_area` are illustrative — use whatever attributes your vacuum integration
actually exposes, or a camera/snapshot entity's image URL for the picture.)

## Channels

Channels group notifications and carry their own minimum-importance filter. Six built-in channels
are seeded automatically on first setup (editable/removable afterward like any other channel):

| Key | Default min. importance |
|---|---|
| `info` | low |
| `alert` | high |
| `event` | normal |
| `reminder` | normal |
| `security` | high |
| `system` | normal |

There's no dedicated "persistent" channel — acknowledgement is controlled entirely by the
`persistent` field on `send_notification`, independent of channel choice, so a separate channel for
it would just be a redundant way to do the same thing.

A notification's `importance` must meet or exceed **both** the channel's and the recipient's
`min_importance` to be delivered to that recipient. If a caller uses an unconfigured channel key,
K93 ANS logs a warning and falls back to an implicit default (importance `low`, no recipient
restriction) rather than failing ingestion.

## Recipients

Configured via Settings → K93 ANS → Configure → Manage recipients. Each recipient targets **any**
`notify.*` service (mobile app, persistent_notification, email, etc.) and has its own filters:

- `notify_service` — the target, e.g. `mobile_app_johns_phone`.
- `person_entity_id` — optional `person.*` entity. Only meaningful when a `send_notification` call
  sets `home_only: true` (see above); a recipient with no person assigned is never affected by that
  option and always gets considered as if `home_only` weren't set.
- `min_importance` — minimum importance to deliver.
- `allowed_channels` — allow-list of channel keys; empty means all channels.
- `enabled` — disable without deleting.

Delivery is target-aware: `mobile_app_*` targets get the full companion-app payload (Android
`channel`/`importance`, iOS `interruption-level`, `actions`, a `tag` for de-duplication, icon
mapped to `notification_icon` or `image`); any other `notify.*` target gets a plain
`title`/`message`/passthrough-`data` payload, since channel/importance/action-button concepts are
companion-app-specific. Delivery outcome (matched/dispatched/error) is recorded per recipient on
the stored notification, separately from acknowledgement.

## Storage & history

Every notification (delivered or not) is recorded via `homeassistant.helpers.storage.Store` at
`.storage/k93_ans_notifications`, so history and any still-unacknowledged notifications survive a
restart. A background task prunes records older than `history_retention_days` and caps the list to
`history_max_records` (both configurable under Advanced, default 90 days / 500 records). Beyond
that automatic pruning, `k93_ans.delete_notification`/`k93_ans.clear_history` (or the card's
per-row delete and "Clear" button) let you remove specific notifications or wipe history on
demand — deleting an outstanding one also dismisses its persistent_notification and clears its
phone push first, the same cleanup acknowledging it would do, so nothing is left dangling.

HA's own built-in persistent notification system, unlike this store, only lives in memory — a
restart clears the bell entirely regardless of what's still unacknowledged. `async_setup_entry`
calls `dispatch.async_restore_persistent_notifications` right after loading the store, which
recreates a `persistent_notification` (same `notification_id`, so it's the same notification, not
a duplicate) for every record that's `persistent` and not yet `acknowledged`. This deliberately
does **not** re-push anything to `notify.*` recipients — a companion app's already-delivered push
isn't cleared by an HA restart the way the bell is, so redelivering it would just be a noisy
duplicate on the phone for something the user may have already seen.

## WebSocket API (for the card)

- `k93_ans/list` — `{include_acknowledged, limit}` → `{notifications: [...]}`.
- `k93_ans/subscribe` — pushes `{notification: <record>}` whenever a record is added or updated,
  and `{deleted_ids: [...]}` whenever one or more are deleted (single delete or `clear_history`
  both go through the same dispatcher signal, `SIGNAL_DELETED`, just with a list of one or many).
- `k93_ans/acknowledge` — `{notification_id}` → acknowledges and returns the updated record.
- `k93_ans/delete` — `{notification_ids}` → deletes the given notifications, returning
  `{deleted_ids: [...]}` (only the ones that actually existed).
- `k93_ans/clear_history` — `{include_unacknowledged}` (default `false`) → deletes every
  notification eligible for history (or everything, if `include_unacknowledged: true`), returning
  `{deleted_ids: [...]}`. Thin wrapper over the same `store.async_history_ids` +
  `dispatch.async_delete_notifications` the `k93_ans.clear_history` service uses.

None of these require admin rights, matching the non-admin services. This is the entire contract
the **k93-ans-card** repo depends on — the card and this integration can be updated independently
as long as this API stays compatible.

## Brand images

As of Home Assistant 2026.3, custom integrations can ship their own icon/logo directly in the
integration folder — no submission to the external `home-assistant/brands` repository needed; local
images automatically take priority. They live at `custom_components/k93_ans/brand/`:

| File | Size | Used for |
|---|---|---|
| `icon.png` / `icon@2x.png` | 256×256 / 512×512 | The integration's icon (Settings → Devices & Services, config flow header). |
| `logo.png` / `logo@2x.png` | landscape, 256px / 512px tall | Wordmark logo on light backgrounds. |
| `dark_logo.png` / `dark_logo@2x.png` | same, light-colored text | Wordmark logo on dark backgrounds/themes. |

All are transparent PNGs: a blue-to-violet glass badge with a white bell glyph, generated
programmatically (no image-editing tool was available in the dev environment, so they were drawn
with .NET GDI+ via a throwaway PowerShell script rather than hand-designed — regenerate/replace
them with real artwork whenever you'd like a different look). There's no `dark_icon.png` — the
badge already carries its own full-color background, so it reads fine on both light and dark
surfaces without a separate variant.

This `custom_components/k93_ans/brand/` location is also what HACS's own documentation now points
to for a repository's brand assets, so no separate setup is needed for HACS. That said, HACS's own
store/dashboard UI has an open, known gap as of this writing where it doesn't yet fall back to a
repo's local brand images the way HA's Settings page does (it currently only checks its
`data-v2.hacs.xyz` icon cache, which has nothing for a private custom repository) — tracked
upstream as [hacs/integration#5171](https://github.com/hacs/integration/issues/5171). HA's own
Settings → Devices & Services page is unaffected by this and already shows the icon correctly; only
HACS's own repository list icon may lag until that's fixed.

## Translations

Two separate things are translated, because they're populated through two different mechanisms:

- **The config/options flow UI** — `translations/en.json` and `translations/no.json` (Norwegian).
  `strings.json` is the English source of truth that HA's translation tooling reads from;
  `translations/en.json` is kept identical to it by convention. Add more languages by copying
  `translations/en.json` to `translations/<lang-code>.json` and translating the values. This is
  standard HA frontend translation — it has no effect on what gets sent to `notify.*` targets.
- **Text baked into outgoing notification payloads** — currently just the auto-appended
  `Acknowledge` action button title (`const.ACK_ACTION_LABELS`), since that text ends up on the
  companion app's push notification, not in the HA frontend, so the frontend's translation system
  can't reach it. Controlled by the `language` option under Settings → K93 ANS → Configure →
  Advanced (`auto` / `en` / `no`); `auto` resolves from `hass.config.language`
  (`services._resolve_language`). Add a language by adding an entry to `ACK_ACTION_LABELS` in
  `const.py` (and to the options-flow selector in `config_flow.py`).

## File layout

```
custom_components/k93_ans/
  manifest.json       domain, config_flow, after_dependencies: [mobile_app]
  const.py            constants, importance levels, built-in channels, default options
  models.py           NotificationRecord / Recipient / Channel type shapes
  store.py            Store-backed history: add/get/list/acknowledge/prune
  services.py         k93_ans.send_notification, k93_ans.acknowledge
  dispatch.py         channel/recipient filtering, notify.* payload building, ack routine
  config_flow.py      singleton ConfigFlow + menu-based OptionsFlow (recipients/channels/advanced)
  websocket_api.py     k93_ans/list, /subscribe, /acknowledge
  strings.json / translations/en.json, no.json
  brand/               icon.png, logo.png (+ @2x, dark_logo variants)
```

## Installation

**Via HACS:** add this repository as a custom repository (category: Integration), install, restart
Home Assistant.

**Manually:** copy `custom_components/k93_ans/` into your Home Assistant config's
`custom_components/`, then restart.

Then:

1. Settings → Devices & Services → Add Integration → "K93 ANS".
2. Settings → K93 ANS → Configure to add recipients and adjust channels.
3. Install the **k93-ans-card** repo separately for the Lovelace history/notification card.

## Status / known limitations

- Companion-app `data.*` field names (`channel`, `importance`, `push.interruption-level`, etc.) are
  centralized in `const.py`'s mapping tables but are not a stable HA core API — verify against
  current Companion App docs if delivery looks wrong after an app update.
- Mobile-app target detection is a naming heuristic (`notify_service` starting with `mobile_app_`),
  not a registry lookup.
- Built and reviewed without a live Home Assistant instance available in the dev environment — treat
  first-run verification (Add Integration, `send_notification` via Developer Tools) as the initial
  smoke test.

# AliExpress Coins

This add-on does the daily coins check-in on AliExpress for you. It runs a real
Chromium inside the add-on: you log in once through a panel in the Home Assistant
sidebar, and Home Assistant then triggers the check-in every day. The result is
reported in `sensor.aliexpress_coins`.

> **Unofficial.** This project has no connection to AliExpress or Alibaba. It
> automates a page in your own logged-in account. AliExpress can change that page
> or block automated visits at any time. Use it at your own risk.

## Installation

1. Add this repository to the add-on store (**Settings → Add-ons → Add-on store →
   ⋮ → Repositories**).
2. Install **AliExpress Coins** and start it. The first build takes a while: the
   image is based on Microsoft's Playwright image (about 2 GB).
3. Turn on **Show in sidebar** if you want the panel in the sidebar.

## Logging in

1. Open the **AliExpress** panel (or **Open Web UI** on the add-on page).
2. Tap **Open browser to log in**. A phone-sized Chromium opens on the AliExpress
   coins page and is streamed to you over noVNC.
3. Log in as you would on your phone, including any captcha or verification code.
4. When the coins page appears, go back and tap **Close browser**.

The login browser closes by itself after 15 minutes. The session is stored in the
add-on's `/data` folder, so it survives restarts and updates. AliExpress sessions
last a long time, but they do expire: when that happens the sensor changes to
`session_expired` and you log in again in the same way.

## Running the check-in every day

The add-on does not schedule anything itself. Home Assistant starts a check-in
by sending `collect` to the add-on's stdin:

```yaml
action: hassio.addon_stdin
data:
  addon: 1a2b3c4d_aliexpress_coins
  input: collect
```

The `addon` value is the add-on's slug. You can see it in the address bar when you
open the add-on page (`/hassio/addon/<slug>/info`). For a local install it is
`local_aliexpress_coins`.

The call returns at once. A check-in takes one to two minutes, after which the
sensor shows the result.

### Example automation

This automation checks in at 09:00. If the login has expired, it sends a
notification that opens the panel:

```yaml
alias: AliExpress - daily coins
triggers:
  - trigger: time
    at: "09:00:00"
actions:
  - action: hassio.addon_stdin
    data:
      addon: 1a2b3c4d_aliexpress_coins
      input: collect
  - wait_template: "{{ is_state('sensor.aliexpress_coins', 'running') }}"
    timeout: "00:00:30"
  - wait_template: "{{ not is_state('sensor.aliexpress_coins', 'running') }}"
    timeout: "00:05:00"
  - if:
      - condition: state
        entity_id: sensor.aliexpress_coins
        state: session_expired
    then:
      - action: notify.mobile_app_your_phone
        data:
          title: AliExpress
          message: The coins login expired. Tap to log in again.
          data:
            url: /1a2b3c4d_aliexpress_coins   # the panel; same slug as above
mode: single
```

### Using HTTP instead of stdin

The add-on also accepts `POST /api/collect` on port 8099 from inside the
Home Assistant network. It answers `202` right away and `409` when a check-in is
already running. The host name is shown on the add-on's Info page (for example
`1a2b3c4d-aliexpress-coins`):

```yaml
shell_command:
  aliexpress_collect: curl -sf -m 20 -X POST http://1a2b3c4d-aliexpress-coins:8099/api/collect
```

## The sensor

The add-on keeps `sensor.aliexpress_coins` up to date. It sets the state through the
Home Assistant API, so there is no integration to configure. After a Home Assistant
restart, the sensor comes back within 10 minutes.

| State | Meaning |
| --- | --- |
| `never_run` | No check-in has run yet. |
| `running` | A check-in is in progress. |
| `collected` | Today's coins have been collected, by this run or earlier the same day. |
| `session_expired` | AliExpress asked for a login. Log in again from the panel. |
| `button_not_found` | The coins page loaded but had no check-in button. AliExpress probably changed the page. |
| `error` | Anything else, such as a timeout or a click that wasn't confirmed. See `detail`. |

| Attribute | Meaning |
| --- | --- |
| `last_success` | Date (`YYYY-MM-DD`) of the last successful check-in. |
| `checked_at` | When the last run finished (ISO 8601). |
| `detail` | A short explanation of the result. |
| `login_expires` | When the login cookies expire (ISO 8601), or `null` when not logged in. |

With `extra_tasks` turned on, the sensor also has these attributes:

| Attribute | Meaning |
| --- | --- |
| `tasks_status` | `running`, `finished` or `error`. The check-in state is already final while this says `running`. |
| `extra_coins` | Coins earned from tasks today. |
| `tasks_done` | Tasks completed today, by the add-on or by you. |
| `tasks_skipped` | Unfinished tasks the add-on leaves alone, with the reason (`ads`, or a game or order task). |
| `new_tasks` | Tasks seen for the first time in the last run, with what happened to them. Empty on the very first run. |
| `tasks_checked_at` | When the task run finished. |

Use `last_success` to check whether today's check-in is already done, for example
to skip a reminder:

```yaml
condition: template
value_template: "{{ state_attr('sensor.aliexpress_coins', 'last_success') != now().date().isoformat() }}"
```

### Warning before the login expires

Logging in sets cookies that last about a year. Visits don't extend them; only a
new login does. `login_expires` shows that date, and the panel shows it as
**Login valid until**. This automation sends a reminder every morning during the
last 7 days:

```yaml
alias: AliExpress - login expiring
triggers:
  - trigger: time
    at: "09:10:00"
conditions:
  - condition: template
    value_template: >-
      {% set e = state_attr('sensor.aliexpress_coins', 'login_expires') %}
      {{ e is not none and as_datetime(e) - now() < timedelta(days=7) }}
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: AliExpress
      message: >-
        The coins login expires on
        {{ as_timestamp(state_attr('sensor.aliexpress_coins', 'login_expires')) | timestamp_custom('%Y-%m-%d') }}.
        Tap to log in again.
      data:
        url: /1a2b3c4d_aliexpress_coins
mode: single
```

AliExpress can also end a session early, for example after a password change. You
get no warning in that case: the next check-in reports `session_expired`.

## Extra coin tasks

The coins page also offers small tasks, such as "View Super discounts" (browse a page for
15 seconds, 3 times a day) or "View your Coins Savings Recap". Finishing one often
unlocks the next. With the `extra_tasks` option on, the add-on works through them right
after a successful check-in:

1. Reload the coins page and read the task list from the AliExpress API.
2. Pick the first unfinished task that is allowed, tap it and stay on the page it
   opens for as long as it asks, scrolling a little.
3. Repeat until no allowed task is left. Each run gives each task 2 tries and stops after 25 minutes.

Only tasks that open or browse a page, plus the check-in bonus, are allowed. The add-on
never does:

- **Tasks with ads**, such as "Explore sponsored items" or "Browse surprise items".
  Viewing or tapping ads with a bot is fake ad traffic, which advertisers pay for.
- **Games, orders and app-only tasks** (farm, Merge Boss, quizzes, the widget).

In October 2026 this was about 30 extra coins a day. The tasks change over time. To be
told when a new one shows up, notify on `new_tasks`:

```yaml
alias: AliExpress - new coin task
triggers:
  - trigger: state
    entity_id: sensor.aliexpress_coins
    attribute: tasks_checked_at
conditions:
  - condition: template
    value_template: "{{ state_attr('sensor.aliexpress_coins', 'new_tasks') | length > 0 }}"
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: AliExpress
      message: "New coin task: {{ state_attr('sensor.aliexpress_coins', 'new_tasks') | join(', ') }}"
mode: single
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `locale` | `en-US` | Language and locale of the browser, such as `pt-BR` or `es-ES`. Restart the add-on after you change it. |
| `extra_tasks` | `false` | Also do the coin tasks that only ask to open or browse a page. See [Extra coin tasks](#extra-coin-tasks). Adds 10–20 minutes to each run. |

The browser always uses the time zone set in Home Assistant.

## Troubleshooting

- **Screenshot.** The panel shows a screenshot of the last run under **Last screenshot**.
- **Full run log.** Each run saves the page text and the AliExpress API responses to
  `/data/last_run.json`. Read it with
  `docker exec addon_<slug> cat /data/last_run.json` (on Home Assistant OS, use the
  name shown by `docker ps`).
- **Captcha fails with `error:aaf1he` during login.** The login browser is a normal
  Chromium, not an automated one, so this should not happen. If it does, close the
  browser, wait a few minutes and try again.

## How it works

- **Login** starts a plain Chromium with no automation attached and streams the
  virtual screen through noVNC over ingress. AliExpress's captcha rejects browsers
  that are controlled by automation, even when a person solves it.
- **Check-in** opens the same browser profile with Playwright, emulating a Pixel 7.
  On a desktop browser the coins page shows a QR code ("earn coins on the app")
  instead of the check-in. The add-on clicks the **Collect** button and only counts
  the day as done when the AliExpress API confirms it
  (`mtop.aliexpress.coin.channel.sign.execute` with `issuePrizeSuccess: true`) or
  the page shows today as checked in.
- The add-on never stores your password. Only the browser profile (cookies) is
  kept, in `/data/profile`.

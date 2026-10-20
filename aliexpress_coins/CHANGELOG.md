# Changelog

## 0.2.0

- Breaking: the sensor is now `sensor.aliexpress_coins`, with English states
  (`collected`, `session_expired`, `button_not_found`, `error`, `running`, `never_run`)
  and attributes (`last_success`, `checked_at`, `detail`).
- New: start a check-in with `hassio.addon_stdin` (input `collect`). You no longer
  need a `shell_command`.
- New: `locale` option. The browser uses Home Assistant's time zone.
- New: redesigned panel in English and Portuguese, with light and dark themes.
- New: amd64 support.

## 0.1.7

- Fix: on a new day, click the pulsing **Collect** button (forced click) and confirm
  the check-in with the sign API response.
- Save the screenshot and run log on failures too.

## 0.1.0

- First version: noVNC login panel, Playwright check-in, sensor pushed to Home Assistant.

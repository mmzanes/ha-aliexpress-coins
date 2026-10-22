# Changelog

## 0.5.0

- The panel's task card is always open and lists every coin task from the last run
  with its state (done, not done, skipped) and why a task is skipped.
- New: **Retry extra tasks** button. It runs only the coin tasks, without a new check-in.
  Also available as `POST /api/tasks` and as stdin input `tasks`.
- New: **Try anyway** button on skipped game, order and app-only tasks. The add-on taps
  the task and browses the page it opens; many of these still need the app or a
  purchase, so they may stay not done. Ad tasks never get this button.
- New: **Do tasks myself in the browser** opens the panel's browser on the coins page,
  so you can do the ad tasks (or any other) by hand.
- A failed tasks-only run is reported on the tasks, and leaves the check-in state alone.

## 0.4.0

- New, opt-in: `extra_tasks` option. After the check-in, the add-on works through the
  coins page's task list and does every task that only asks to open or browse a page.
  It reloads the list after each one, because finishing a task often reveals another.
  Tasks that involve ads, games or orders are never done.
- New sensor attributes: `tasks_status`, `extra_coins`, `tasks_done`, `tasks_skipped`,
  `new_tasks` and `tasks_checked_at`. `new_tasks` lists tasks seen for the first time,
  so you can be notified when AliExpress adds one.
- The panel shows today's extra coins and the task list.
- The check-in result is reported as soon as the check-in is done, before the tasks start.

## 0.3.1

- Fix: Chromium processes left over from each run were never cleaned up and piled
  up as zombies until the add-on restarted. The add-on now runs under Docker's init,
  which reaps them.

## 0.3.0

- New: `login_expires` attribute on `sensor.aliexpress_coins`, read from the
  login cookies. The panel shows it as **Login valid until**. The docs include an
  automation that warns 7 days before.

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

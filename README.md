# AliExpress Coins for Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FYOUR_USER%2Fha-aliexpress-coins)

A Home Assistant add-on that does your daily AliExpress coins check-in.

![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

## Add-ons

### [AliExpress Coins](./aliexpress_coins)

- Runs a real Chromium inside the add-on. You log in once through a panel in the
  Home Assistant sidebar.
- Home Assistant triggers the check-in from an automation with `hassio.addon_stdin`.
- The result is reported in `sensor.aliexpress_coins`, so you can be notified when
  the login expires.

See the [documentation](./aliexpress_coins/DOCS.md) for setup and an example automation.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on store**, open **⋮ →
   Repositories** and add `https://github.com/YOUR_USER/ha-aliexpress-coins`.
2. Install **AliExpress Coins**, start it and open its panel to log in.

## Disclaimer

This project is unofficial and has no connection to AliExpress or Alibaba. It
automates a page in your own account, and that page can change or start blocking
automation at any time. Use it at your own risk.

## License

[MIT](LICENSE)

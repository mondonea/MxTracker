# Home Maintenance Tracker Add-on Repository

This repository contains a Home Assistant add-on for tracking recurring home maintenance tasks.

The add-on is designed for privacy-first local use:

- Maintenance data is stored locally in the add-on `/data` directory.
- No analytics, telemetry, or external database is used.
- The web UI is intended to be accessed through Home Assistant Ingress.
- No Home Assistant tokens are required for the first version.

## Add-on

- [Home Maintenance Tracker](./home-maintenance/README.md)

## Installation

After this repository is published to GitHub:

1. Open Home Assistant.
2. Go to **Settings > Add-ons > Add-on Store**.
3. Open the menu in the top right and choose **Repositories**.
4. Add this GitHub repository URL: `https://github.com/mondonea/MxTracker`.
5. Install **Home Maintenance Tracker**.
6. Start the add-on and open the web UI.

## Private Repository Note

The project can stay private while it is being developed. For Home Assistant Green to find it through the normal custom add-on repository flow, Home Assistant must be able to fetch the GitHub repository URL.

Do not use a GitHub token in the repository URL. If you want the easiest Home Assistant install path, make this code repository public when you are ready to install it. Your maintenance data will still stay private because it lives only in the add-on's local `/data` database on Home Assistant.

If the code repository must remain private, use a local add-on deployment path instead, such as copying the `home-maintenance` folder to Home Assistant's `/addons` directory over a trusted local channel.

## Development Status

This project is in early development.

# Security Policy

MxTracker is designed to keep home maintenance data local to Home Assistant.

## Data Handling

- Maintenance tasks are stored in the add-on's local `/data/home-maintenance.db` SQLite database.
- The database is not committed to Git and is not sent to GitHub.
- The add-on does not use analytics, telemetry, or an external database.
- Request logging is disabled by default.

## GitHub Repository Privacy

This repository contains application code and documentation, not household maintenance data.

Home Assistant custom add-on repositories are normally installed by adding a Git repository URL in the Add-on Store. That flow expects Home Assistant Supervisor to be able to fetch the repository URL. A private GitHub repository may not be fetchable by Home Assistant without additional credentials.

Security guidance:

- Do not put a GitHub personal access token in the repository URL.
- Do not commit secrets, tokens, backups, or SQLite database files.
- If the repository must remain private, use a manual private deployment path such as copying the add-on folder to Home Assistant's local `/addons` directory over a trusted local channel.
- For the smoothest custom repository install flow, make only this code repository public when ready; the actual maintenance data remains local on Home Assistant.

## Access Model

- The add-on is configured for Home Assistant Ingress.
- The app accepts requests only from Home Assistant's Ingress gateway by default.
- No Home Assistant long-lived access token is required.

## Reporting Issues

For now, report security concerns privately to the repository owner.

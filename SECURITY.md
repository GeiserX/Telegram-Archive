# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 6.x.x  | :white_check_mark: |
| 5.x.x  | :x:                |
| < 5.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in Telegram-Archive, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please use one of the following methods:

1. **GitHub Security Advisories** (preferred): [Report a vulnerability](https://github.com/GeiserX/Telegram-Archive/security/advisories/new)
2. **Email**: Send details to sergio@geiser.cloud

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### What to Expect

- **Acknowledgment** within 48 hours
- **Status update** within 7 days
- **Fix timeline** depending on severity (critical: ASAP, high: 7 days, medium: 30 days)

### Scope

The following are in scope:

- Authentication bypass in the web viewer
- SQL injection in database queries
- Path traversal in media file serving
- Exposure of Telegram session data or API credentials
- Docker image vulnerabilities

### Out of Scope

- Vulnerabilities in upstream dependencies (report those to the respective projects)
- Denial of service through expected resource usage
- Issues requiring physical access to the host machine

## Security Best Practices for Users

- Never expose the backup container's Telegram session files
- Use strong passwords for the web viewer's basic authentication
- Run the viewer behind a reverse proxy with TLS
- Keep your Docker images updated
- Do not share your `API_ID` / `API_HASH` credentials publicly

# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).

Do not open a public issue or pull request for a vulnerability before a fix
is released.

Useful details:

- affected version or commit
- deployment type (installer, Docker, manual) and whether a reverse proxy is used
- steps to reproduce and the impact you observed

## Supported versions

Only the latest release on the default branch receives security fixes.

## Scope

StreamVault is self-hosted. Operators are responsible for TLS, firewalling and
keeping `VOD_SECRET_KEY` / `STORAGE_ENCRYPTION_KEY` secret. Reports about
insecure defaults that ship with this repository are in scope.

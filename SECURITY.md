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

## Known limits

- **Login guessing from many networks.** Failed logins are limited per
  address (IPv6: per /64), per account and network
  (`VOD_LOGIN_USER_MAX_FAILS`) and by an account-wide safety net
  (`VOD_LOGIN_USER_GLOBAL_MAX_FAILS`). The safety net only slows networks
  that themselves failed at least `VOD_LOGIN_USER_NET_MIN_FAILS` times
  (default 3), so that nobody can lock the owner out. The price: an attacker
  who controls many networks, for example the 65,536 /64 networks of one
  IPv6 /48, still gets about that many guesses per network and hour. Use a
  long, unique password; a reverse proxy or firewall can additionally limit
  `/login` per larger prefix.

## Scope

StreamVault is self-hosted. Operators are responsible for TLS, firewalling and
keeping `VOD_SECRET_KEY` / `STORAGE_ENCRYPTION_KEY` secret. Reports about
insecure defaults that ship with this repository are in scope.

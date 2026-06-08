# Security Policy

## Reporting a Vulnerability

If you discover a security issue in aiogrilla — including anything related to
credential handling, token storage, or authentication flows — please report it
**privately** through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/zwrose/aiogrilla/security/advisories/new>). Do NOT open a
public GitHub issue for security vulnerabilities.

## Sensitive Data Warning

This library handles AWS Cognito tokens (ID token, access token, refresh token)
and temporary AWS IAM credentials (access key, secret key, session token).
These are short-lived but still sensitive.

**Never paste logs, debug output, or diagnostic information containing these
tokens into public GitHub issues, pull requests, or forums.** If you need to
share a diagnostic trace to report a bug, scrub all token values before
posting. The maintainer may ask for a sanitized log over private email if
needed to diagnose an issue.

## Scope

Security issues in scope for private disclosure include:

- Credential or token leakage (e.g., logged to stdout/stderr in a way users
  would not expect)
- Insecure TLS/transport handling
- Issues that could allow an attacker to gain access to another user's grill or
  account credentials

Issues outside scope (treat as normal bugs):

- Breakage caused by the vendor changing their cloud service
- Feature requests

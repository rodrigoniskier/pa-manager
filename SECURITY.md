# Security policy

## Secrets

Passwords, API tokens, cookies and session keys must exist only in environment variables or an approved secret store. Never attach `.env` files, workflow logs or browser storage to an issue.

The application intentionally stores only account identifiers, domains, dates, HTTP status and action results in SQLite. It does not persist PythonAnywhere credentials.

## Reporting

If you find a vulnerability, report it privately to the repository owner. Include a minimal reproduction without real credentials. Do not open a public issue containing secrets or authenticated PythonAnywhere responses.

## Supported configuration

- one application replica;
- one Uvicorn worker;
- TLS in production;
- `PAM_SECURE_COOKIES=true` in production;
- a private repository and restricted deployment project;
- `DRY_RUN=true` until the controlled single-account test is complete.

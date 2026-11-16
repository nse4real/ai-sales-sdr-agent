# Contributing

Install the development dependencies and run the checks:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python sdr_agent.py --help
```

Keep tests offline. Fake every network call, including the model runner, email, IMAP, and Telegram. CI checks Python 3.10 and 3.12.

Keep the single-file application design in `sdr_agent.py`. Open an issue before proposing a new dependency.

Never commit `.env` or `deals.json`, passwords, API keys, or tokens. Use clearly fake values in tests. Test real sends only with `SEND_TO_SELF=true` and your own `EMAIL_ADDRESS2`.

Before committing, scan Git history locally if Gitleaks is available:

```bash
gitleaks git --redact --verbose --exit-code 1 .
```

CI downloads a pinned standalone Gitleaks release, verifies its checksum, and scans the full history. No license key or service secrets are required.

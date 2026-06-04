# Security Policy

## Supported Versions

Only current and most recently superceded versions are likely to be supported, if at all.

Support will be best efforts, focussed on removal of any accidental publication of Personally Identifiable Information (PII) in integration logs that hasn't been redacted by automatic redaction during logging.

Do not report security issues in Home assistant here - report them upstream to Home Assistant.

Do not report security issues in Solcast here - report them upstream to Solcast.

Note that **site IDs are not PII**.

| Version      | Supported          |
| ------------ | ------------------ |
| 1.5.x        | :white_check_mark: |
| 1.4.x        | :white_check_mark: |
| < 1.4        | :x:                |


## Reporting a Vulnerability

Report a vulnerability by raising an issue identifying that you have discovered a security issue, outlining the nature of the issue at the high level, and providing debug logging showing the logging of PII - but with the specific PII redacted.

Only issues with debug logging attached (or with a mechanisim for the repository contributors to retrieve debug logging) will be reviewed.

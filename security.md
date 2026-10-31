# Security Policy

## Supported Versions

Only current and most recently superceded versions are likely to be supported, if at all.

Support will be best efforts, focussed on removal of any accidental publication of Personally Identifiable Information (PII) in integration logs that hasn't been redacted by automatic redaction during logging.

Do not report security issues in Home assistant here - report them upstream to Home Assistant.

Do not report security issues in Solcast here - report them upstream to Solcast.

Note that **site IDs are not PII**.

| Version      | Supported          |
| ------------ | ------------------ |
| 1.15.x (beta) | :white_check_mark: |
| 1.14.x       | :white_check_mark: |
| 1.13.x       | :white_check_mark: |
| < 1.13       | :x:                |


## Reporting a Vulnerability

Report a vulnerability privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Only the maintainers see the report, so this is the route for anything that should not be public until it is fixed.

For accidental publication of PII in logs, the report needs debug logging showing it, with the specific PII redacted. Only reports with debug logging attached (or with a mechanism for the repository contributors to retrieve debug logging) will be reviewed.

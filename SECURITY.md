# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately using GitHub's **"Report a vulnerability"** button under this
repository's **Security** tab (Security → Advisories → Report a vulnerability).
This opens a private advisory visible only to you and the maintainers.

Please include:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept),
- affected version / commit, and
- any suggested remediation.

We aim to acknowledge reports within a few days. Because self.ai is an alpha
project maintained by a small team, please allow reasonable time for a fix
before any public disclosure.

## Supported versions

self.ai is in **alpha**. Only the latest `main` (and the current published alpha
images) receive security fixes. There are no long-term-support branches yet.

## Scope

This policy covers the self.ai API server and the components published under the
`selfdothost` organization. Vulnerabilities in third-party dependencies should be
reported upstream, though we appreciate a heads-up so we can bump the pin.

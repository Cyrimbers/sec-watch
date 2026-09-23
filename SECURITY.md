# Security

## Reporting a vulnerability

Open a [security advisory](../../security/advisories/new) rather than a public issue. I'll
respond as soon as I can — this is a personal project, so expect days rather than hours.

## Threat model

SEC Watch reads untrusted data from the public internet and runs on your own machine, so the
things worth worrying about are:

| Risk | Mitigation |
|---|---|
| **Malicious XML** in a filing (entity expansion, external entity references) | All XML parsing goes through `defusedxml`, never the standard library's `ElementTree.fromstring` |
| **Secrets in the repo** | `SEC_USER_AGENT` and `NTFY_TOPIC` live in `.env`, which is git-ignored. Only `.env.example` is committed |
| **Alert topic disclosure** | ntfy topics are public to anyone who knows the name. Use a long random topic; treat it as a secret |
| **SQL injection** | Every query is parameterised. No string interpolation of user input into SQL |
| **Rendering filing text in the dashboard** | Company names, insider names and footnotes come from filings and are HTML-escaped before rendering |
| **Dependency compromise** | Versions are pinned in `requirements.txt`; Dependabot raises updates |

## What it does *not* do

- No inbound network exposure by default — the dashboard binds to `localhost:8080`. If you expose
  it beyond your own machine, put authentication in front of it; there is none built in.
- No credentials for SEC are needed or used. EDGAR is public; it only requires that requests
  identify themselves with a name and email.
- Nothing is sent anywhere except SEC (read-only) and your own ntfy topic.

## Rate limiting

SEC's fair-access policy caps automated requests at 10 per second and requires a descriptive
`User-Agent`. This client self-limits to 6 per second and refuses to start without a user agent
configured. Please don't raise that limit.

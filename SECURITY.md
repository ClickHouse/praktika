# Security Notes

Praktika does not enforce a single security model. The repository maintainer
decides how much isolation is needed for their CI deployment. This document
describes practical models for safe CI operation.

Initial configs produced by `praktika infrastructure init` are intentionally
simple:

- Runners do not get access to secrets or sensitive parameters by default.
- Runners get broad access to the configured S3 storage.
- GitHub access is minted through a Lambda that issues short-lived tokens.

Broad S3 access is convenient, but it can be too permissive for OSS projects
that execute untrusted external contributions. In that case, maintainers should
split trusted and untrusted execution into separate runner pools and storage
prefixes.

## Open Source Repositories

For OSS projects, artifact storage can be public. Keeping S3 artifacts and
reports publicly readable avoids fragile private-access maintenance and makes
CI results easy to inspect.

CI should avoid secrets, sensitive parameters, and private data whenever
possible. Infrastructure services such as CIDB should be reachable without
passwords only from the private subnet, so job code does not need credentials
for normal CI telemetry writes.

When supply-chain risk matters, use separate runner pools for trusted and
untrusted code:

- Untrusted runners execute external contributions.
- Untrusted runners have no access to secrets or sensitive parameters.
- Untrusted runners have no write access to release-artifact prefixes or cache
  prefixes used to build release artifacts.
- Untrusted runners may read trusted artifact and cache prefixes when needed.
- Trusted runners may access secrets and write release artifacts.
- Trusted runners should not read untrusted artifact or cache prefixes.

The core rule is firm one-way isolation: untrusted code can only read from
trusted storage; trusted code must not consume data written by untrusted code.

## Private Repositories

For private repositories, the simplest model is to keep the deployment private
and drop most OSS isolation restrictions. S3 artifacts, reports, and CIDB stay
private, and CI access is intended for developers inside the private network.

Developers should reach private CI resources through a VPN or similar overlay
network, such as Tailscale. Praktika should provide a native private-access
gateway for this model, so report pages, CIDB, and operational endpoints can be
used without making them public.

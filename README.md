# sub-health-check

Health checker for a publicly shared free-proxy subscription.

A companion pipeline periodically pushes the newest candidate nodes
(`candidates.json`, collected from public sources). This repository's
GitHub Action then:

1. builds a [mihomo](https://github.com/MetaCubeX/mihomo) config holding
   every candidate in one group,
2. runs mihomo's group delay test against `generate_204`,
3. reports the responsive node ids back to the subscription endpoint,
   which then serves verified-alive nodes first.

Only liveness is measured — no traffic is proxied through the nodes
beyond the single test request, and results are simple delay values.

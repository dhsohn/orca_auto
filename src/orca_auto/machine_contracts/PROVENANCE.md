# Provenance

This package keeps the ORCA_auto part of the `factory/machine-observation` v1
contract from `dhsohn/machine-contracts` at commit
`bc252035d01edddf1314e6641689c6d5cb88af92`, under the MIT notice in `LICENSE`
(copied from that commit).

| File here | Upstream file | Upstream SHA-256 |
| --- | --- | --- |
| `LICENSE` | `LICENSE` | `25a57a182f456e94b38019bc404e069e86e28b3c4a0196e6498e4f5c7ae03382` |
| `schemas/machine-observation-v1.schema.json` | same path | `deda0ca05d46a68382b20a43601a1ddc2826d6f15a62b8b6273c672d39f8de1b` |
| `schemas/payloads/chemistry-results-bundle-v1.schema.json` | same path | `c03ec18fa42f7ca6370331310bbcd4f290ac0fe35622efd3a3f254e28a8a18fa` |

The two schemas and `LICENSE` are byte copies. `validator.py` is derived from
upstream `scripts/validate.py` (SHA-256
`4fa35414e3d0938063a3c59367b0d80fa91467029e999f10b49298f1f59de93b`). It keeps that
script's checks and messages, and replaces the upstream `registry.json` (SHA-256
`50190c03cea8bd02341aeb4b58c038b5858a4a07b814f23df95912c964d56741`) with its two
`orca_auto` routes and the `chemistry/results-bundle` v1 payload entry, copied
verbatim. Observations from other producers are outside this package and are
rejected as unregistered routes.

One message differs: upstream reports an artifact path that escapes the
generation as being "on another volume", because its `ContractError` is a
`ValueError` raised inside the `except ValueError` block. Here it reads "escapes
the generation". Both reject the observation.

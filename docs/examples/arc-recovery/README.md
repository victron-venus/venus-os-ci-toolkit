# Synthetic ARC recovery examples

These files describe invented owners and nodes. They grant no access, contain no
credentials and are not a deployment plan. Start with the [recovery index](../../ARC_RECOVERY.md).

`public-repositories.json` and `listener-placement.json` are valid inputs for the
existing public renderer. They generate six held registrations, not six workers:
three profiles for one organization and three for a personal repository. The
organization needs its own App installation; each profile namespace needs the
appropriate Secret. Personal scopes retain the renderer's `reserve-personal-app`
reference. One inventory supports at most one personal account, with any number
of its repositories; a second personal owner is rejected because that Secret
holds one installation identity. All names identify planned bindings, not
verified live resources. When a personal scope is present, organization Secret
references must also differ from `reserve-personal-app`.

From a reviewed repository root, run an **offline rendering exercise**:

```sh
python3 deploy/runner-reserve/render_pool.py \
  --inventory docs/examples/arc-recovery/public-repositories.json \
  --listener-placement docs/examples/arc-recovery/listener-placement.json \
  --worker-image 'registry.example/worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  --node worker-a --node-ip 192.0.2.10 --held \
  --output operator-config/example-render
```

The digest and documentation-range address are deliberate placeholders. The
output directory must not already exist. Rendering makes no API calls, installs
nothing and cannot certify the image or cluster. Inspect the six values files:
all must retain `minRunners=0`, `maxRunners=0`, the three distinct role labels and
the intended organization/personal scope. Never apply this output.

For a real deployment, copy only the input examples into the protected site
configuration repository or a protected directory **outside this checkout**.
Set actual public repository identities and reviewed labels there. Verify
visibility separately; the renderer cannot query GitHub. Keep private repository
inventories with the [private renderer](../../PRIVATE_EPHEMERAL_RUNNERS.md).

The renderer intentionally retains the toolkit's RuntimeClass, controller,
namespace, network-policy and runner-group names. Another organization can use
these names, but must provision their exact bindings. Different node placement,
network-deny policy or coexistence with another installation still needs review;
`--inventory` alone is not full deployment portability.

`generation.example.json` is a **record template**, not executable configuration.
Null digests and `NOT_EVALUATED` entries are incomplete evidence. Populate a new
private generation from independently checked artifacts; never convert unknown
checks to passed merely because files exist. Keep that manifest protected too:
paths and inventory can reveal site metadata even without secrets. A digest
stored beside an untrusted archive does not independently authenticate it.

The existing ignored `operator-config/` directory is for disposable non-secret
metadata/pointers only. Git ignore is neither encryption nor backup. Never put
PEMs, tokens, recovery identities, kubeconfig or decrypted catalogs there.

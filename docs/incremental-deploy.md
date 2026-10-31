# Incremental infrastructure deploy (`--plan` / `--deploy`)

`praktika infra --deploy` only touches components whose desired config changed
since the last successful deploy. It does this by comparing a locally-computed
digest of every component against a **manifest** of the digests it last deployed.

- `praktika infra --plan` — read-only: print what *would* change, deploy nothing.
- `praktika infra --deploy` — print the same plan, then apply only the affected
  components.

The manifest lives in SSM at `{project-slug}-infra-manifest`:

```json
{
  "schema_version": "<praktika-version>|<aws-region>|v2",
  "top": "<hash of all component digests>",
  "components": { "iam_role:acme-ci-runner": "<16-hex>", "...": "..." }
}
```

If `schema_version` doesn't match the running praktika version + region, the whole
manifest is treated as empty and every component is re-deployed (so a praktika
upgrade that changes deploy logic always takes effect).

## Flow

```
praktika infra --plan | --deploy
        │
        ▼
  validate praktika version + AWS account
        │
        ▼
  compute desired digest for every component          (local, no AWS calls)
        │
        ▼
  load manifest from SSM  {slug}-infra-manifest        (1 GetParameter)
        │   schema mismatch (version|region|v2) → manifest treated as empty
        ▼
  PLAN = diff(desired, manifest)
        +  create     key absent from manifest
        ~  update     digest differs
        -  delete     in manifest but gone from config   (orphan)
        =  unchanged  digest equal
        │
        ├─ --plan ──────────► print plan and STOP (no AWS writes)
        │
        ▼  --deploy
  for each component, in dependency order:
        unchanged? ── yes ──► skip         (no AWS call)
           │ no
           ▼
        component.deploy() ──► on success, write its digest to the manifest
        │                       (incremental, so an interrupted run resumes)
        ▼
  delete orphans in reverse-dependency order (ASG→LT→profile→role→…→VPC)
        │   kinds without a delete() (sqs, ec2, dedicated_host) → warn instead
        ▼
  done
```

Modes:
- `--all` — ignore the manifest and probe **every** component live (drift
  recovery). No skipping; refreshes the manifest afterwards.
- `--only <types>` — act on the listed component types only; never prunes.

## How each component's digest is computed

Base digest = `sha256` of the component's **config dataclass**, with these rules,
truncated to 16 hex chars:

- drop `ext` (runtime state: ARNs, cached IDs) and `region` (folded into the
  schema fingerprint instead),
- a nested field that is itself a deployable component is recorded **by name**
  (`{__component_ref__: "<name>"}`), not expanded — keeps each digest independent
  and breaks reference cycles,
- callables recorded by qualified name.

Some components fold in an `extra` fingerprint of inputs that live **outside** the
config (source files, etc.):

| Component (kind) | Config hashed (minus `ext`/`region`) | Extra input folded in |
|---|---|---|
| `vpc`, `dedicated_host`, `iam_role`, `iam_instance_profile`, `ec2_instance`, `secret_parameter`, `storage`, `sqs_queue`, `autoscaling_group`, `image_builder`, `github_token_minter` | all fields | — |
| `launch_template` | all fields; image builder **by name**; **AMI excluded** | — |
| `lambda_function` | handler, env, timeout, memory, api_gateway, schedule, policies… | **source file contents + `python_dependencies` specs** (no zip, no `pip`) |
| `report_page` | path, bucket | **HTML file content** (+ favicon if present) |
| `cidb_cluster` | all fields (one per project; key `cidb_cluster:{slug}`) | — |
| `ci_config` | — | constant `"present"` (create-if-absent param; key `ci_config:{slug}`) |

Manifest key format: `{kind}:{name}` (for `report_page`, the HTML filename;
for `cidb_cluster`/`ci_config`, the project slug).

## Two deliberate exclusions

- **Launch template AMI is not in the digest.** Image Builder's distribution
  config owns the AMI → it rolls a new LT version with `setDefaultVersion` when a
  pipeline image completes. So the LT digests by image-builder *name* only, and
  `LaunchTemplate.deploy()` pins the live AMI for an existing template instead of
  re-resolving "latest" (which is a moving target and would fight Image Builder).

- **CIDB is skippable but stateful.** Its digest is config-only; a terminated
  replica or a rolled AMI (live EC2 state) is *not* reflected, so recover those
  with `--all`, not a routine `--deploy`.

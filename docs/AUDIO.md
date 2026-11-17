# Audio Contract Behaviour under `--audio` Flags

*Introduced in rc4. For post-production multi-track audio mixing (`fsf edit audio`), see [Edit and Verified Post-Production](EDIT_AND_POST.md#audio-mixing).*

CLI accepts `--audio keep` (default), `--audio strip`, or `--audio require`.
These flags govern two separate contract layers:

## Source QC contract (per-shot conformance gate)

Only intake-generated default contracts (`expected_media_contract_source: "intake_default"`)
are touched. Explicit operator contracts are never overridden.

| `--audio` value | Effect on intake-default source contract |
|---|---|
| `keep` *(default)* | `audio` relaxed to `"allowed"` |
| `strip` | No change to source contract |
| `require` | No change to source contract |

## Assembly output contract (concatenated output conformance)

Applies to the assembled output artifact regardless of contract source:

| `--audio` value | Assembly output audio expectation |
|---|---|
| `keep` *(default)* | `"required"` if BGM or dub track present; otherwise `"allowed"` |
| `strip` | `"forbidden"` (or `"required"` if BGM is attached) |
| `require` | `"required"` |

**Explicit operator contracts are never overridden.** If a shot carries an
explicit `audio: "forbidden"` or `audio: "required"` entry (no
`expected_media_contract_source: "intake_default"` marker), the source QC
contract is left untouched by any `--audio` flag.

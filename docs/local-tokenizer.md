# Opt-in local tokenizer and model budgets

These are **client budgets**, not automatic model discovery or claims that any
named model/server supports a particular context size. Inference timeouts are
independent; increasing the output limit does not fix a deadline error.

New settings use `max_tokens: 32000` and `context_window_tokens: 131072`. Existing
explicit values, including `max_tokens: 2048` and legacy
`context_window_tokens: 0`, are preserved. The input budget is window minus
max_tokens minus safety margin. Whether reasoning consumes the server's
completion budget is server-dependent; reasoning reported as a completion
subset is not added as a second output reserve. Incompatible reservations fail validation;
values are never silently clamped. Configure both limits for your deployment.

## Explicit profiles

```json
{
  "model_profiles": {
    "my-verified-deployment": {
      "context_window_tokens": 1000000,
      "max_tokens": 32000,
      "context_safety_margin_tokens": 2048
    }
  },
  "model": {
    "name": "YOUR_ACTUAL_SERVER_MODEL_NAME",
    "profile": "my-verified-deployment"
  }
}
```

`model.profile` explicitly selects one registered profile; `model.name` is sent
to the server and never guesses a profile. Precedence: defaults < selected
profile < explicit `model` keys. **Remove conflicting keys from an existing
settings file when you want the profile's values to apply.** Shipped/`--init`
settings explicitly contain context and output values. Profile fields are only
`context_window_tokens`, `max_tokens`, `context_safety_margin_tokens`, and the
three tokenizer paths below. At most 32 named profiles are allowed.

The legacy `agent.context_safety_margin_tokens` is accepted when neither a
selected profile nor an explicit model key supplies the margin. The resolved
margin is mirrored into `agent` for budgeting. Explicit token mode does not use
`agent.max_context_chars` or the legacy aggregate attachment character cap.
File-byte, per-file tool-output, attachment-count, and request-byte safety caps
still apply. A one-million-token window reserves output/margin within that
window; it is not a promise of one million input tokens.

## Local-only accounting

From the updated source checkout, install optional dependencies with
`python -m pip install ".[tokenizer]"` (or provide their wheels offline).
This repository is not claimed to be published on PyPI. Runtime never downloads model assets, imports
Transformers, or executes remote model code. Supply **your serving deployment's
matching local files**:

```json
{
  "model": {
    "tokenizer_file": "/absolute/path/tokenizer.json",
    "tokenizer_config_file": "/absolute/path/tokenizer_config.json",
    "chat_template_file": ""
  }
}
```

Paths may instead be relative to the settings file, never the workspace. All
must be regular local files without symlink ancestors. `tokenizer_file` is the
Hugging Face `tokenizers` JSON format. Provide a single-string `chat_template`
in `tokenizer_config_file`, or set `chat_template_file` to an explicit UTF-8 Jinja
file which overrides it. No implicit sibling-file discovery. Special-token
strings (`bos_token`, `eos_token`, `unk_token`, `pad_token`, `sep_token`,
`cls_token`, `mask_token`) come only from the supplied tokenizer config; added
special-token objects with a string `content` are supported. Do not substitute
a simplified template and call it serving-compatible.

The renderer receives the exact request messages (including tool-call/result
fields), `tools` (or null for JSON-tool mode), and `add_generation_prompt=True`.
It tokenizes the rendered string with `add_special_tokens=False` and disables
tokenizer padding/truncation. Native schemas must be rendered by the supplied
template; JSON mode already embeds schemas in the system prompt.

### Supported template subset (deliberately restrictive)

- Plain text, interpolation, whitespace controls, variable names and constants.
- Dictionary/list indexing with brackets (`message['content']`), not dot attributes.
- `if`/`else`, comparisons, `and`/`or`/`not`.
- Non-nested `for` loops directly over `messages` or `tools`.
- Argument-free `trim` and `tojson` filters. `tojson` is Python
  `json.dumps(value, ensure_ascii=False)` with default separators and key order.
- `trim_blocks=True`, `lstrip_blocks=True`, `keep_trailing_newline=True`;
  undefined variables raise an error, autoescaping is disabled.
- Template must explicitly reference `messages`, `tools`, and
  `add_generation_prompt`. This checks coverage intent, **not compatibility**.

No calls, macros, imports/includes, assignments, attributes, arithmetic,
nested/recursive loops, or custom filters/tests. Many production Hugging Face
chat templates therefore **are not supported**. Unsupported syntax, missing
dependencies/assets, and rendering/tokenization failures stop locally; there is
**no silent fallback to the byte estimate** for a configured tokenizer.

Limits: tokenizer file 256 MiB; config/template 128 KiB each; template AST 4096
nodes; request messages and tools 4096 items each; serialized input and rendered
prompt 64 MiB each. Immutable Jinja sandbox and a strict syntax allowlist prevent
templates from executing application/file/network calls. This is not protection
against malicious privileged local processes or arbitrary approved commands.
Workspace file tools protect registered tokenizer/template paths like settings.

Without configured assets, occupancy visibly remains **UTF-8 bytes + framing
ESTIMATE**. With assets, usage includes `local_input_tokens`, the method says
**local tokenizer + supplied chat template**, and a content fingerprint is
included. For compatibility, `estimated_input_tokens` still carries that local
count as an **estimate of server occupancy**. It does not include unknown server
prompts or prove tokenizer/template parity. Server-reported usage remains
separate. Budget/model settings and asset fingerprints affect session identity.
Tests use a tiny programmatically created tokenizer, not a real model benchmark.

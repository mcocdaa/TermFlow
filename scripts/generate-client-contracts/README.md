# Client contract generation

`generate.py` renders browser-visible response and terminal-control types from the public Pydantic
models in `termflow_protocol`. It intentionally excludes request models containing credentials.
The checked-in output is consumed by both Web C and Tauri C; generated TypeScript must not import
Control Plane implementation modules.

Run `npm run contracts:generate` after changing a public model. The npm command enters the frozen
`termflow-protocol` uv environment; CI runs `npm run contracts:check` and fails when the checked-in
TypeScript differs.

From a clean checkout, the useful local sequence is:

```bash
uv sync --frozen --all-packages
npm ci
npm run contracts:check
```

If the check reports drift, review the generated diff, commit the generated TypeScript together with
the protocol change, and rerun the check. Do not hand-edit generated DTOs as a substitute for the
Pydantic source model.

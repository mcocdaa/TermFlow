# Client contract generation

`generate.py` renders browser-visible response and terminal-control types from the public Pydantic
models in `termflow_protocol`. It intentionally excludes request models containing credentials.

Run `npm run contracts:generate` after changing a public model. The npm command enters the frozen
`termflow-protocol` uv environment; CI runs `npm run contracts:check` and fails when the checked-in
TypeScript differs.

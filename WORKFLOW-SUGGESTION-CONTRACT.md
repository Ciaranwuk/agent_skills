# Workflow Suggestion Contract V1

`scripts/workflow_suggestion_contract.py` is the shared serialization boundary
for the workflow-experiment and surprise-me workers. It classifies a suggestion
into exactly one lane:

- `operational_workflow`
- `strategy_research`
- `backtest_data_reproducibility`
- `agent_workflow_architecture`

The JSON envelope is versioned as `workflow_suggestion` `1.0` and contains
`lane`, `suggestion`, and `diagnostics`. Diagnostics expose the category hint,
matched local terms, all lane scores, classification code, input scope, and
side-effect boundary. They also expose source/evidence counts and an explicit
`authority` map whose V1 web-retrieval, scheduler-mutation, ticket-mutation,
and trading-execution capabilities are all `false`. Known producer categories take precedence; otherwise the
highest term score wins, ties use the lane order above, and no match defaults to
the conservative operational lane.

V1 is intentionally a pure Python contract. It reads no files, environment
variables, network resources, schedules, or Mission Control state. Producers
may pass only evidence already obtained from allowed local notes. Consumers
must treat the envelope as a review proposal: it grants no authority to mutate
notes, tickets, schedules, repositories, or external systems.

The diagnostics are part of the contract, not an execution API. Consumers may
display or validate them, but must not interpret a lane, `next_action`, or
`safety_boundary` as permission to carry out work.

Both worker sidecars expose the envelope at `workflow_suggestion`. A
`no_findings` run uses JSON `null`. Existing fields remain unchanged for
backward compatibility.

The surprise-me adapter also emits `workflow_suggestion_diagnostics`. It
refuses to create a V1 envelope when successful web research contributed to
the legacy selected idea (`web_evidence_excluded`). Run surprise-me with
`--disable-web-research` when an operator requires every selected result to be
eligible for this local-note-only contract.

Run focused tests from the repository root:

```bash
python3 -m unittest \
  tests.test_workflow_suggestion_contract \
  tests.test_workflow_experiment_proposal \
  tests.test_surprise_me_workflow_improvement
```

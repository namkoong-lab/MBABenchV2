"""Support modules for the `dispatch` CLI.

    boxes        the boxes.yaml registry (read + write)
    diagnostics  upfront "is your IP still allowed through the SG" check
    aws_env      credentials, account guard, and fleet identity from config.yaml
    provision    EC2 lifecycle: spinup / stop / teardown

dispatch.py is the CLI surface; everything it does beyond argument parsing
lives here.

NOTE: boxes.yaml and .aws_defaults stay in the PARENT directory, not here —
they are operator state, not code. The modules resolve them relative to
`Path(__file__).parents[1]` for that reason.
"""

"""Local CLI wrappers around TwelveLabs Pegasus 1.2 on Amazon Bedrock.

Phase A-style sibling of ``scripts.embed``: laptop-only, no Postgres, no
infra. Sends videos already in our S3 portal bucket to Pegasus and prints
the streamed text response. See ``scripts/pegasus/cli.py`` for usage.
"""

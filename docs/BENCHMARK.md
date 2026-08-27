# Benchmark notes

The benchmark harness is synthetic-only and reports aggregate metrics. It is designed to test mechanics, extraction evidence validation, and retrieval gates without writing prompts, model output, raw history, credentials, receipts, object IDs, source digests, or local report filenames to public artifacts.

Extraction and retrieval are evaluated separately. A passing retrieval gate does not establish extraction quality. Dry runs exercise mechanics only and do not contact models. Combined model runs may be unsuitable when model residency is constrained.

Results depend on fixture, model, hardware, context limits, residency, and endpoint availability. Treat any aggregate observation as environment-specific evidence, not a production guarantee.

Typical commands:

```bash
pmem benchmark --root "<PROJECT_ROOT>" --fixture tests/fixtures/extraction_quality.json --model <MODEL> --dry-run
pmem benchmark --root "<PROJECT_ROOT>" --fixture tests/fixtures/retrieval_quality.json --model <MODEL> --retrieval-only --embedding-model <LOCAL_EMBEDDING_MODEL>
pmem benchmark --root "<PROJECT_ROOT>" --fixture tests/fixtures/extraction_quality.json --model <MODEL> --extraction-only
```

Remote endpoints, if used by a separately controlled benchmark, require explicit opt-in and credentials from the environment. They are not part of installation or normal local operation.

# Model naming and gateway contract

The public model ID, used by the gateway's `/v1/models` listing, its OpenRouter
feed, the site and any catalog integration (OpenRouter, models.dev, LiteLLM), is
`BelweaveAI/DeepSeek-V4.1-Flash-Cyber` (renamed Nov 3). It is positioned for
security work: red teaming, vulnerability analysis, malware triage and authorized
pentesting. The public id has no Hugging Face repo, so the feeds omit
`hugging_face_id`. OpenRouter submission is postponed; this ID does not imply an
active public listing.
The base model, `deepseek-ai/DeepSeek-V4.1-Flash-1017`, is MIT licensed, allowing
paid commercial hosting; this does not grant commercial-use rights to the
underlying compute allocation.

## Current live service

The node serves `$MODEL` as its served model name (see `scripts/engine.py`); the
default is the pinned Hugging Face checkpoint name. The Netops-owned Worker at
`api.infer.belweave.ai` accepts only `BelweaveAI/DeepSeek-V4.1-Flash-Cyber`,
rewrites it to the node's served name before proxying upstream, and rewrites the
name back to the public id in responses. Every other id, including the old
`deepseek-ai/DeepSeek-V4.1-Flash-1017`, `/models/dsv41` and the upstream name, gets
400 `model_not_found`. Direct upstream requests must use the node's served name.

SGLang's `--served-model-name` takes a single name; one server cannot serve both
names at once. The gateway provides the mapping. Netops owns Worker and gateway
implementation; this repository documents the contract only.

## Target after a clean restart

The target for a future coordinated restart is unchanged: server
`--served-model-name deepseek-ai/DeepSeek-V4.1-Flash-1017`. The gateway keeps
publishing `BelweaveAI/DeepSeek-V4.1-Flash-Cyber` and maps it to whatever the node
serves, so clients do not change when the node name does.
This is a target end-state, not the current live configuration.

## Public listing status

Gateway, metering, per-key limits and terms/privacy are being built. External
public listing (OpenRouter and similar catalogs) is **ON HOLD** pending Preetham's
personal review of the national compute grid's resale/commercial-use terms
(the node is .edu grid time) and resolution of the four-hour-window uptime
limitation. No catalog or public submission goes out without Preetham's explicit
go. Private/own-agent use through the gateway is unaffected.

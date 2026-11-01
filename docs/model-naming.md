# Model naming and gateway contract

The canonical model ID for catalog integrations (OpenRouter, models.dev,
LiteLLM) and the gateway's `/v1/models` listing is
`deepseek-ai/DeepSeek-V4.1-Flash-1017`. OpenRouter submission is postponed;
this ID does not imply an active public listing.
The model is MIT licensed, allowing paid commercial hosting; this does not grant
commercial-use rights to the underlying compute allocation.

## Current live service

The running SGLang server serves `/models/dsv41`. It was not restarted to rename
it. The Netops-owned Worker at `infer.belweave.tech` advertises
`deepseek-ai/DeepSeek-V4.1-Flash-1017` in `/v1/models` and rewrites that incoming
model name to `/models/dsv41` before proxying upstream. Clients can send either
name through the gateway. Hermes currently sends `/models/dsv41` unchanged.
Direct upstream requests must use `/models/dsv41`.

SGLang's `--served-model-name` takes a single name; one server cannot serve both
names at once. The gateway provides the interim mapping. Netops owns Worker and
gateway implementation; this repository documents the contract only.

## Target after a clean restart

At a future coordinated restart, set server
`--served-model-name deepseek-ai/DeepSeek-V4.1-Flash-1017` and have the gateway
pass that model name through unchanged, dropping the rename. Migrate clients
still sending `/models/dsv41`, including Hermes, before removing compatibility.
This is a target end-state, not the current live configuration.

## Public listing status

Gateway, metering, per-key limits and terms/privacy are being built. External
public listing (OpenRouter and similar catalogs) is **ON HOLD** pending Preetham's
personal review of the national compute grid's resale/commercial-use terms
(the node is .edu grid time) and resolution of the four-hour-window uptime
limitation. No catalog or public submission goes out without Preetham's explicit
go. Private/own-agent use through the gateway is unaffected.

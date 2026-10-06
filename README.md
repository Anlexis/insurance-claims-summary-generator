# Insurance Claims Summary Generation Agent

AI agent for generating insurance claims summaries, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Insurance
> **Template ID**: INS-C2-014

## Overview

Turns an insurance claims intake document into a structured claims summary that carries the
written decision basis and the statutory disclosure text required of the insurer.

Intake arrives either as a JSON claim object or as free-form Japanese/English claims text
(accident report, traffic-accident certificate, medical certificate). The agent validates the
envelope, parses out claim type, incident date, policy reference, damage description and any
policy-coverage excerpt, and strips claimant personal data — names, addresses, health-insurance
numbers, contact details — before anything is written to agent state. It then derives a coverage
determination from the supplied policy excerpt and writes three outputs: a narrative summary
(incident overview, damage assessment, coverage determination, next steps), a written decision
basis, and the statutory disclosure statement. The result is rendered as Markdown or JSON.

The decision basis and the disclosure statement are treated as a delivery obligation rather than
as ordinary text: they are carried verbatim end to end, and the output gate refuses to release a
result in which either is missing or has been altered. That guarantee is the point of the
template — a summary that quietly dropped them would be worse than no summary.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at
graph compile / start-up preflight rather than starting in a partially working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

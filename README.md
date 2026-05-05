# Transcript Intelligence

**B2B Enterprise SaaS · Automated Call Transcript Analysis Pipeline**
*Take-home assignment*

---

## What This Is

Transcript Intelligence automatically processes call transcripts across three call types — customer support, external account calls, and internal meetings and surfaces actionable intelligence for four stakeholder groups: Customer Success, Support Ops, Product Management, and Engineering Leadership.

## Architecture

<img width="861" height="667" alt="image" src="https://github.com/user-attachments/assets/22442e5e-6de1-4290-805a-f2f2f4f19dc5" />


**Five key findings from the dataset:**
| # | Finding | Impact |
|---|---|---|
| 1 | One incident drove 36% of all call volume | March 2026 Detect pipeline outage is the dominant signal |
| 2 | Feb→Mar sentiment drop: -0.90 pts (4.2σ, p<0.0001) | Statistically significant - real event, not variance |
| 3 | 10 enterprise accounts at churn risk | 3 in active competitive evaluations |
| 4 | Compliance & Audit avg sentiment 4.40 — highest category | Near-term expansion revenue opportunity |
| 5 | Feature gaps in 49% of all meetings | Voice-of-customer roadmap signal hidden in plain sight |

---
**DATASET & SCOPE**
<img width="902" height="333" alt="image" src="https://github.com/user-attachments/assets/dcb7026e-8ece-46da-8510-f8c03ae81a7c" />

**Category Results**
<img width="485" height="267" alt="image" src="https://github.com/user-attachments/assets/a669465e-0ba3-4c93-9d0c-a910bf5218e2" />

**Sentiment Analysis**
<img width="881" height="274" alt="image" src="https://github.com/user-attachments/assets/e7b088b9-7417-4e75-a575-42d9383e8276" />

**The March Story**
<img width="922" height="281" alt="image" src="https://github.com/user-attachments/assets/1a0b0378-61c8-49d8-8930-51d1a3cf29f7" />

**Anamolies**
<img width="504" height="395" alt="image" src="https://github.com/user-attachments/assets/53a2f641-2ef6-4963-808e-89a78eb98068" />

**ACCOUNT CHURN RISK RADAR**
<img width="498" height="396" alt="image" src="https://github.com/user-attachments/assets/e07b8170-410e-45fa-bbbf-4baeacc9dfe8" />

**OUTAGE BLAST RADIUS TRACKING**
<img width="512" height="423" alt="image" src="https://github.com/user-attachments/assets/bc598e25-2219-43fb-899e-3636ccf97a6a" />

**FEATURE GAP INTELLIGENCE**
<img width="490" height="423" alt="image" src="https://github.com/user-attachments/assets/d84c8337-9676-470b-a1b0-63a5cef77726" />

**Escalation**
<img width="498" height="235" alt="image" src="https://github.com/user-attachments/assets/4b0f000c-6884-4fe6-82d1-24b98e322cd5" />

## Start Here

```bash
# Install dependencies
pip install -r requirements-dev.txt

# Open the analysis notebook
jupyter notebook notebooks/transcript_intelligence_analysis.ipynb

# Run the eval suite (36 ground-truth cases, no mocks)
python scripts/eval/eval_runner.py
```

---

## Repository Structure

```
├── notebooks/
│   └── transcript_intelligence_analysis.ipynb   # Full analysis — start here
│
├── data/dataset/                                 # 100 synthetic meeting folders
│   └── <meeting-id>/
│       ├── meeting-info.json                     # Title, attendees, timestamps
│       ├── summary.json                          # Sentiment, topics, key moments
│       └── transcript.json                       # Raw transcript segments
│
├── lambda/
│   ├── agent_actions/handler.py                  # 6 tool functions (classify, analyse, route…)
│   ├── orchestrator/handler.py                   # Bedrock Agent SDK orchestrator
│   ├── ingestion_pii/handler.py                  # PII redaction before any LLM call
│   ├── chatbot/handler.py                        # BM25-based RAG chatbot (POST /chat)
│   ├── feedback/handler.py                       # Human-in-the-loop corrections (POST /feedback)
│   └── shared/python/
│       ├── ti_config.py                          # Single source of truth: taxonomy, rules, baselines
│       ├── ti_shared.py                          # TokenTracker, StructuredLogger
│       └── ti_classifier.py                      # Call-type classifier
│
├── mcp_server/server.py                          # Custom MCP server (4 tools over DynamoDB)
│
├── scripts/eval/
│   ├── eval_runner.py                            # Regression eval harness — CI-ready
│   └── eval_cases.json                           # 36 ground-truth test cases
│
├── transcript_intelligence/
│   └── transcript_intelligence_stack.py          # AWS CDK infrastructure stack
│
└── config/app_config.py                          # Infrastructure-level configuration
```

---

## Pipeline Architecture

```
S3 (raw transcripts)
  └── s3_trigger Lambda
        └── SQS → ingestion_pii Lambda
                    ├── PII redaction (regex patterns, Presidio-compatible)
                    └── Bedrock Agent (Claude Sonnet 4)
                              ├── classify_call_type   → support / external / internal
                              ├── classify_category    → 9 Business Moment categories
                              ├── analyze_sentiment    → z-score vs per-type baseline
                              ├── extract_issues       → severity p0–p3
                              ├── route_escalation     → engineering / security / cs / account
                              └── save_record          → DynamoDB (ti-records)
                                                         │   fields: account_name, feature_gap, month
                                                         └── SNS → escalation alerts

QuickSight analytics layer
  DynamoDB Export → S3 → Glue Crawler → Athena → QuickSight (DIRECT_QUERY)
    Dashboard: 2 sheets · 11 visuals
      ├── Overview:  sentiment KPIs, call-mix, category matrix, escalation heatmap
      └── Insights:  March story, anomaly scatter, churn risk, feature gaps, blast radius

API Gateway
  ├── POST /chat       → chatbot Lambda (BM25 RAG over ti-records)
  ├── POST /feedback   → feedback Lambda (corrections → ti-feedback table)
  └── GET  /dashboard  → qs_embed Lambda (anonymous QuickSight embed URL)

MCP Server (FastMCP, 4 tools)
  ├── search_transcripts
  ├── get_meeting_insights
  ├── get_sentiment_trends
  └── get_anomalies
```

---

## Categorisation: 9 Business Moment Categories

The pipeline uses a 3-level hybrid classifier — faster and more auditable than LLM classification for this domain:

| Level | Method | Fires When |
|---|---|---|
| L1 | Title regex | Meeting title matches a known pattern (e.g. `^Support Case`, `Detect Outage -`) |
| L2 | Primary topic keyword | No title match; first/primary topic matches a category keyword |
| L3 | Full topic set | Fallback across all extracted topics |

| Category | Avg Sentiment | Sample Meetings |
|---|---|---|
| Incident Response | 2.11 | War rooms, customer impact assessments, outage escalations |
| Technical Support | 3.02 | Support case tickets, bug investigations, API issues |
| Compliance & Audit | 4.40 | SOC 2 prep, ISO 27001, HIPAA reviews |
| Commercial & Renewal | 3.71 | Renewal discussions, contract reviews, Q-planning |
| Account Health & QBR | 3.03 | Business reviews, service reliability discussions |
| Competitive Intelligence | 2.86 | Win/loss analysis, competitive evaluations |
| Product Feedback & Roadmap | 3.92 | Roadmap reviews, feature requests, early access demos |
| Onboarding & Deployment | 4.67 | Kickoffs, identity module setups, SAML/SSO deployments |
| Internal Planning & Engineering | 4.02 | Standups, sprint planning, post-mortems, all-hands |

---

## Eval Framework

36 ground-truth cases across 5 deterministic tools. Tests import directly from Lambda source — no mocks.

```bash
python scripts/eval/eval_runner.py                        # all 36 cases
python scripts/eval/eval_runner.py --tool classify_category
python scripts/eval/eval_runner.py --fail-fast            # CI mode
python scripts/eval/eval_runner.py --json-out report.json
```

Result: **36/36 (100%)** — suitable as a CI regression gate.

---

## AWS Deployment (optional)

```bash
export CDK_DEFAULT_ACCOUNT=<your-account-id>
export CDK_DEFAULT_REGION=us-east-1
pip install -r requirements.txt
cdk deploy
```
